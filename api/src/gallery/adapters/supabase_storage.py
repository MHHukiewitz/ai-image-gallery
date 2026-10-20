"""ObjectStore on Supabase Storage.

The client carries the user's JWT, so the bucket policies confine every call to the caller's own
`{user_id}/` folder.

Storage sits behind a small database connection pool on the free tier, and a burst of uploads
and grid polls can exhaust it. It answers `429 too_many_connections`, or a 5xx, for a moment, so
every call is retried with jittered backoff before the error reaches the caller.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Sequence

import httpx
from storage3.exceptions import StorageApiError

from gallery.adapters.supabase_client import AsyncClient

logger = logging.getLogger(__name__)

# 429 is "too many connections"; the 5xx codes are Storage or its database failing for a moment.
# Everything else, such as 400, 403 and 404, is a request that a retry can't fix.
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
_TRANSIENT_NETWORK_ERRORS = (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException)
_MAX_ATTEMPTS = 4
_BACKOFF_BASE_SECONDS = 0.2
_BACKOFF_CAP_SECONDS = 2.0

_DUPLICATE_STATUS = 409


def _status_of(error: BaseException) -> int | None:
    """Return the HTTP status of a Storage error. Storage sends it as a string, such as "429"."""
    if not isinstance(error, StorageApiError):
        return None
    try:
        return int(error.status)
    except (TypeError, ValueError):
        return None


def _transient_reason(error: Exception) -> str | None:
    """Return a short description if retrying `error` could help, else None."""
    if isinstance(error, _TRANSIENT_NETWORK_ERRORS):
        return type(error).__name__
    status = _status_of(error)
    if status in _TRANSIENT_STATUSES:
        return str(status)
    return None


class SupabaseObjectStore:
    def __init__(
        self,
        client: AsyncClient,
        bucket: str,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._bucket = client.storage.from_(bucket)
        # Injected so tests can run the retries without waiting or randomness.
        self._sleep = sleep
        self._uniform = uniform

    async def put(self, path: str, data: bytes, content_type: str) -> None:
        """Write a new object. The bucket has no update policy, so an existing key is an error."""
        attempts = 0

        async def upload() -> None:
            nonlocal attempts
            attempts += 1
            try:
                await self._bucket.upload(path, data, {"content-type": content_type})
            except StorageApiError as error:
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
        """Sign all paths in one request.

        A path that can't be signed, such as a missing object, is left out of the result.
        """
        if not paths:
            return {}
        entries = await self._with_retries(
            "signed_urls", lambda: self._bucket.create_signed_urls(list(paths), ttl_seconds)
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
        return urls

    async def _with_retries[T](self, operation: str, call: Callable[[], Awaitable[T]]) -> T:
        """Run `call`, retrying transient Storage errors with exponential backoff and full jitter.

        Full jitter spreads the retries of a burst over the whole backoff window instead of
        sending them back together, which is what keeps a saturated pool saturated.
        """
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                return await call()
            except Exception as error:
                reason = _transient_reason(error)
                if reason is None or attempt == _MAX_ATTEMPTS:
                    raise
                delay = self._uniform(
                    0, min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
                )
                logger.warning(
                    "storage call failed, retrying",
                    extra={
                        "operation": operation,
                        "status": reason,
                        "attempt": attempt,
                        "delay_seconds": delay,
                    },
                )
                await self._sleep(delay)
        raise AssertionError("unreachable: the last attempt returns or raises")
