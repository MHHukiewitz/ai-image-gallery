"""ObjectStore on Supabase Storage.

The client carries the user's JWT, so the bucket policies confine every call to the caller's own
`{user_id}/` folder.

Storage sits behind a small database connection pool on the free tier, and a burst of uploads
and grid polls can exhaust it. It answers `429 too_many_connections`, or a 5xx, for a moment, so
every call is retried with jittered backoff before the error reaches the caller.

The database caps the objects one user may hold in the bucket (migration
`20261020000017_storage_quota.sql`). A put past that cap raises QuotaExceededError, not retried.
"""

import asyncio
import logging
import random
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence

from storage3.exceptions import StorageApiError

from gallery.adapters.retry import (
    TRANSIENT_NETWORK_ERRORS,
    TRANSIENT_STATUSES,
    RetryPolicy,
    retry_transient,
)
from gallery.adapters.supabase_client import AsyncClient
from gallery.domain.errors import QuotaExceededError

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 4
_BACKOFF_BASE_SECONDS = 0.2
_BACKOFF_CAP_SECONDS = 2.0

_DUPLICATE_STATUS = 409

# The SQLSTATE enforce_storage_quota raises when the caller's folder holds the limit. storage-api
# (v1.82) doesn't answer it with 429 the way PostgREST does: it sends 500 with the message
# "database error, code: PT429" and drops the database's wording. A version that follows PostgREST
# would send the code itself. Matching the code, not the status, keeps a gateway's own 429 or a
# real database failure from reading as "storage full", and stops the 500 from being retried.
_QUOTA_REFUSED_CODE = "PT429"
_STORAGE_QUOTA_MESSAGE = "You've reached your storage limit. Delete some images to make room."
# Unlike the daily upload quota, this one counts what the user holds, not a 24-hour window: a
# slot is free the moment they delete an image. A day's Retry-After would tell a client to give up
# on what one delete fixes, so this only asks it not to retry straight away.
_STORAGE_QUOTA_RETRY_AFTER_SECONDS = 60

# A cached URL is reused only while it has at least this long left, so a page that holds one
# never shows a link that dies before the user clicks it.
_CACHE_MIN_VALIDITY_SECONDS = 10 * 60
_CACHE_MAX_ENTRIES = 5_000

type CacheKey = tuple[str, str, int]


class SignedUrlCache:
    """A thread-safe LRU of signed URLs keyed by `(bucket, path, ttl_seconds)`.

    The lock guards plain dict operations and is never held across an `await`, so the same
    instance is safe from worker threads and from many tasks on the event loop.
    """

    def __init__(
        self,
        *,
        max_entries: int = _CACHE_MAX_ENTRIES,
        min_validity_seconds: float = _CACHE_MIN_VALIDITY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_entries = max_entries
        self._min_validity_seconds = min_validity_seconds
        self._clock = clock
        self._entries: OrderedDict[CacheKey, tuple[str, float]] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def get(self, key: CacheKey) -> str | None:
        """Return the URL if it stays valid for the minimum time, and mark it recently used."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            url, expires_at = entry
            if expires_at - self._clock() < self._min_validity_seconds:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return url

    def put(self, key: CacheKey, url: str, ttl_seconds: int) -> None:
        with self._lock:
            self._entries[key] = (url, self._clock() + ttl_seconds)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)


# One cache for the whole process, shared by the adapter that every request builds. Sharing it
# across users is safe because a path reaches `signed_urls` only from a row the caller's own RLS
# let them read (or from their own upload), so a hit can't give a request a URL for a file that
# request couldn't have signed itself. Don't feed this cache paths that did not come from such a
# row.
_signed_urls = SignedUrlCache()


def _status_of(error: BaseException) -> int | None:
    """Return the HTTP status of a Storage error. Storage sends it as a string, such as "429"."""
    if not isinstance(error, StorageApiError):
        return None
    try:
        return int(error.status)
    except (TypeError, ValueError):
        return None


def _is_storage_quota_refusal(error: StorageApiError) -> bool:
    return error.code == _QUOTA_REFUSED_CODE or f"code: {_QUOTA_REFUSED_CODE}" in str(error.message)


def _transient_reason(error: Exception) -> str | None:
    """Return a short description if retrying `error` could help, else None."""
    if isinstance(error, TRANSIENT_NETWORK_ERRORS):
        return type(error).__name__
    status = _status_of(error)
    if status in TRANSIENT_STATUSES:
        return str(status)
    return None


class SupabaseObjectStore:
    def __init__(
        self,
        client: AsyncClient,
        bucket: str,
        *,
        cache: SignedUrlCache | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._bucket_name = bucket
        self._bucket = client.storage.from_(bucket)
        self._cache = _signed_urls if cache is None else cache
        # Injected so tests can run the retries without waiting or randomness.
        self._sleep = sleep
        self._uniform = uniform

    async def put(self, path: str, data: bytes, content_type: str) -> None:
        """Write a new object. The bucket has no update policy, so an existing key is an error.

        Raises QuotaExceededError when the caller's folder already holds as many objects as the
        database allows.
        """
        attempts = 0

        async def upload() -> None:
            nonlocal attempts
            attempts += 1
            try:
                await self._bucket.upload(path, data, {"content-type": content_type})
            except StorageApiError as error:
                if _is_storage_quota_refusal(error):
                    logger.info("storage refused an upload past the object quota")
                    raise QuotaExceededError(
                        _STORAGE_QUOTA_MESSAGE,
                        retry_after_seconds=_STORAGE_QUOTA_RETRY_AFTER_SECONDS,
                    ) from error
                # After a timeout or a 5xx we can't tell whether the object landed. Our keys
                # carry a fresh uuid per upload, so a duplicate on a retry is our own first try.
                if attempts > 1 and _status_of(error) == _DUPLICATE_STATUS:
                    return
                raise

        await self._with_retries("put", upload)

    async def get(self, path: str) -> bytes:
        return await self._with_retries("get", lambda: self._bucket.download(path))

    async def delete(self, paths: Sequence[str]) -> None:
        """Remove the objects. A path that doesn't exist is not an error."""
        if paths:
            await self._with_retries("delete", lambda: self._bucket.remove(list(paths)))

    async def signed_urls(self, paths: Sequence[str], ttl_seconds: int) -> dict[str, str]:
        """Sign all paths, reusing cached URLs and asking Storage for the rest in one request.

        A path that can't be signed, such as a missing object, is left out of the result.
        """
        urls: dict[str, str] = {}
        misses: list[str] = []
        for path in dict.fromkeys(paths):
            cached = self._cache.get((self._bucket_name, path, ttl_seconds))
            if cached is None:
                misses.append(path)
            else:
                urls[path] = cached
        if misses:
            urls.update(await self._sign(misses, ttl_seconds))
        return urls

    async def _sign(self, paths: list[str], ttl_seconds: int) -> dict[str, str]:
        entries = await self._with_retries(
            "signed_urls", lambda: self._bucket.create_signed_urls(paths, ttl_seconds)
        )
        urls: dict[str, str] = {}
        for entry in entries:
            signed_url = entry["signedURL"]
            if entry["error"] or not signed_url:
                logger.warning(
                    "could not sign an object url",
                    extra={"path": entry["path"], "reason": entry["error"]},
                )
                continue
            urls[entry["path"]] = signed_url
            self._cache.put(
                (self._bucket_name, entry["path"], ttl_seconds), signed_url, ttl_seconds
            )
        return urls

    async def _with_retries[T](self, operation: str, call: Callable[[], Awaitable[T]]) -> T:
        """Run `call`, retrying transient Storage errors with backoff and full jitter."""
        return await retry_transient(
            call,
            operation=operation,
            policy=RetryPolicy(_MAX_ATTEMPTS, _BACKOFF_BASE_SECONDS, _BACKOFF_CAP_SECONDS),
            transient_reason=_transient_reason,
            logger=logger,
            message="storage call failed, retrying",
            sleep=self._sleep,
            uniform=self._uniform,
        )
