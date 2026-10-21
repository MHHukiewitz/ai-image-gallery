"""ImageRepo on PostgREST.

The client carries the user's JWT, so RLS decides which rows exist for the caller. Another
user's image reads as missing, never as forbidden.

A database request that stalls or fails for a moment (a timeout, a 429 or 5xx, a lost connection)
is retried, but only the calls that are safe to repeat: reads, and the RPCs that set a value
instead of adding a row. Inserts and deletes are never retried, because the first attempt may
have landed.
"""

import asyncio
import functools
import logging
import random
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any, Concatenate
from uuid import UUID

from postgrest import APIError, CountMethod

from gallery.adapters.retry import RetryPolicy, retry_transient, transient_database_reason
from gallery.adapters.supabase_client import AsyncClient
from gallery.domain.errors import QuotaExceededError
from gallery.domain.models import (
    AllImages,
    AnalysisStatus,
    GalleryQuery,
    ImageRecord,
    NewImage,
    Page,
    SimilarTo,
    TextQuery,
)

logger = logging.getLogger(__name__)

_IMAGES = "images"
_METADATA = "image_metadata"
_DETAILS = "image_details"
_UPLOAD_LOG = "upload_log"
# A SHA-256 hex digest as the API stores it: 64 lowercase hex characters.
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")

# The SQLSTATE enforce_upload_quota raises when an insert would pass the daily limit. PostgREST
# answers a PT<status> state with that HTTP status and keeps the code in the error body. Matching
# the code, not the 429, keeps a gateway's own rate limit from reading as "quota used up".
_QUOTA_REFUSED_CODE = "PT429"
# The window the database counts uploads over (24 hours), so the longest wait for a free slot.
_QUOTA_WINDOW_SECONDS = 24 * 60 * 60

# Two retries after the first try. The request timeout has already waited out a stalled attempt,
# so the pauses between tries stay short.
_RETRY_POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=0.2, cap_delay_seconds=1.0)


def _retry_idempotent[**P, R](
    operation: str,
) -> Callable[
    [Callable[Concatenate["SupabaseImageRepo", P], Awaitable[R]]],
    Callable[Concatenate["SupabaseImageRepo", P], Awaitable[R]],
]:
    """Retry the decorated repo method on transient database failures.

    Only for methods that are safe to run twice: a read, or an RPC whose second run leaves the
    same state as the first. Never an insert or a delete.
    """

    def decorate(
        method: Callable[Concatenate["SupabaseImageRepo", P], Awaitable[R]],
    ) -> Callable[Concatenate["SupabaseImageRepo", P], Awaitable[R]]:
        @functools.wraps(method)
        async def retrying(self: "SupabaseImageRepo", *args: P.args, **kwargs: P.kwargs) -> R:
            return await retry_transient(
                lambda: method(self, *args, **kwargs),
                operation=operation,
                policy=_RETRY_POLICY,
                transient_reason=transient_database_reason,
                logger=logger,
                message="database call failed, retrying",
                sleep=self._sleep,
                uniform=self._uniform,
            )

        return retrying

    return decorate


def _row_to_record(row: dict[str, Any]) -> ImageRecord:
    """Map one `image_details` row to a record. A search RPC adds `score` to the same shape."""
    return ImageRecord(
        id=row["id"],
        user_id=UUID(row["user_id"]),
        filename=row["filename"],
        original_path=row["original_path"],
        thumbnail_path=row["thumbnail_path"],
        mime_type=row["mime_type"],
        size_bytes=row["size_bytes"],
        width=row["width"],
        height=row["height"],
        content_sha256=row["content_sha256"],
        uploaded_at=datetime.fromisoformat(row["uploaded_at"]),
        status=AnalysisStatus(row["ai_processing_status"]),
        tags=tuple(row["tags"] or ()),
        description=row["description"],
        colors=tuple(row["colors"] or ()),
        error=row["last_error"],
        score=row.get("score"),
    )


class SupabaseImageRepo:
    def __init__(
        self,
        client: AsyncClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._client = client
        # Injected so tests can run the retries without waiting or randomness.
        self._sleep = sleep
        self._uniform = uniform

    async def insert(self, image: NewImage) -> ImageRecord:
        """Insert the image and its pending metadata, then read the record back.

        Raises QuotaExceededError when the database refuses the row because the user has reached
        the daily quota. The service checks the quota first, so this is what's left: two uploads
        that raced past that check, or an API whose quota settings differ from the database's.
        """
        try:
            inserted = (
                await self._client.table(_IMAGES)
                .insert(
                    {
                        "user_id": str(image.user_id),
                        "filename": image.filename,
                        "original_path": image.original_path,
                        "thumbnail_path": image.thumbnail_path,
                        "mime_type": image.mime_type,
                        "size_bytes": image.size_bytes,
                        "width": image.width,
                        "height": image.height,
                        "content_sha256": image.content_sha256,
                        "stored_sha256": image.stored_sha256,
                    }
                )
                .execute()
            )
        except APIError as error:
            if error.code != _QUOTA_REFUSED_CODE:
                raise
            logger.info("the database refused an upload past the daily quota")
            raise QuotaExceededError(
                error.message or "You've reached today's upload limit. Try again tomorrow.",
                retry_after_seconds=_QUOTA_WINDOW_SECONDS,
            ) from error
        image_id: int = inserted.data[0]["id"]
        try:
            await (
                self._client.table(_METADATA)
                .insert(
                    {
                        "image_id": image_id,
                        "user_id": str(image.user_id),
                        "ai_processing_status": "pending",
                    }
                )
                .execute()
            )
        except Exception:
            # PostgREST can't wrap both inserts in one transaction. An image without metadata
            # is hidden by image_details and never analyzed, so don't leave the row behind.
            await self._discard_image_row(image_id)
            raise
        record = await self.get(image_id)
        if record is None:
            raise RuntimeError(f"image {image_id} is not readable right after its insert")
        return record

    async def _discard_image_row(self, image_id: int) -> None:
        try:
            await self._client.table(_IMAGES).delete().eq("id", image_id).execute()
        except Exception:
            logger.exception(
                "could not remove an images row after its metadata insert failed",
                extra={"image_id": image_id},
            )

    @_retry_idempotent("get")
    async def get(self, image_id: int) -> ImageRecord | None:
        # retry(False): the SDK's own retry of a 503 would stack on top of ours.
        response = (
            await self._client.table(_DETAILS)
            .select("*")
            .eq("id", image_id)
            .limit(1)
            .retry(False)
            .execute()
        )
        return _row_to_record(response.data[0]) if response.data else None

    @_retry_idempotent("find_by_sha256")
    async def find_by_sha256(self, sha256s: Sequence[str]) -> dict[str, ImageRecord]:
        """Find the caller's images by either hash, in one query.

        A hash matches an image whose `content_sha256` (the file as uploaded) or `stored_sha256`
        (the file as stored, without metadata) equals it. RLS limits the view to the caller's
        rows, so another user's copy of the same file is not a match. Rows come back newest upload
        first, and the first row per hash is kept.
        """
        if not sha256s:
            return {}
        # The hashes go into PostgREST's filter syntax, so only plain hex may get there. The API
        # schema already enforces this; a bad value here would be a bug, not user input.
        for sha256 in sha256s:
            if not _SHA256_PATTERN.fullmatch(sha256):
                raise ValueError(f"not a SHA-256 hex digest: {sha256!r}")
        listed = ",".join(sha256s)
        response = (
            await self._client.table(_DETAILS)
            .select("*")
            .or_(f"content_sha256.in.({listed}),stored_sha256.in.({listed})")
            .order("last_uploaded_at", desc=True)
            .order("id", desc=True)
            .retry(False)
            .execute()
        )
        wanted = set(sha256s)
        matches: dict[str, ImageRecord] = {}
        for row in response.data:
            record = _row_to_record(row)
            # One image can answer two of the given hashes, one through each column.
            for sha256 in {row["content_sha256"], row.get("stored_sha256")} & wanted:
                matches.setdefault(sha256, record)
        return matches

    async def bump(self, image_id: int) -> ImageRecord | None:
        """Move the image to the top of the gallery. None when it is missing or not the caller's."""
        records = await self.bump_many([image_id])
        return records[0] if records else None

    async def bump_many(self, image_ids: Sequence[int]) -> list[ImageRecord]:
        """Move the images to the top of the gallery, the last id on top, in one RPC and one read.

        Returns the images that moved, in the order given. An id that is missing or not the
        caller's is left out. The RPC and the read are retried on their own, so a lost read does
        not run the bump again.
        """
        if not image_ids:
            return []
        bumped = await self._bump_images(list(image_ids))
        if not bumped:
            return []
        return await self._read_in_order(bumped)

    @_retry_idempotent("bump_images")
    async def _bump_images(self, image_ids: list[int]) -> list[int]:
        """Call the RPC and return the ids it bumped. Running it again restamps them in order."""
        response = await self._client.rpc("bump_images", {"p_image_ids": image_ids}).execute()
        # The RPC returns the bumped ids as a bare list. Compare strictly (a bool is an int in
        # Python) so a surprising payload bumps nothing instead of passing.
        data = response.data
        if not isinstance(data, list):
            return []
        return [image_id for image_id in data if type(image_id) is int]

    @_retry_idempotent("read_bumped")
    async def _read_in_order(self, image_ids: list[int]) -> list[ImageRecord]:
        """Read the images in one query and return them in the order of `image_ids`."""
        response = (
            await self._client.table(_DETAILS)
            .select("*")
            .in_("id", image_ids)
            .retry(False)
            .execute()
        )
        by_id = {row["id"]: _row_to_record(row) for row in response.data}
        return [by_id[image_id] for image_id in image_ids if image_id in by_id]

    @_retry_idempotent("list")
    async def list(self, query: GalleryQuery, *, limit: int, offset: int) -> Page[ImageRecord]:
        """Return one page: most recently uploaded first for `AllImages`, best match for a search.

        The search functions return the `image_details` columns (all but `last_uploaded_at`) plus
        `score`, so their rows map like the view's. They come back in rank order, and this method
        never re-sorts them. A text query that carries an embedding goes to
        `search_images_semantic`, which matches words and meaning. Without one it goes to
        `search_images`, which matches words only.
        """
        # Ask for one row more than the page, to learn whether another page follows.
        fetch = limit + 1
        if isinstance(query, AllImages):
            response = (
                await self._client.table(_DETAILS)
                .select("*")
                .order("last_uploaded_at", desc=True)
                .order("id", desc=True)
                .range(offset, offset + limit)
                .retry(False)
                .execute()
            )
        elif isinstance(query, TextQuery) and query.embedding is not None:
            response = await self._client.rpc(
                "search_images_semantic",
                {
                    "q": query.q,
                    "q_embedding": list(query.embedding),
                    "lim": fetch,
                    "off": offset,
                },
            ).execute()
        elif isinstance(query, TextQuery):
            response = await self._client.rpc(
                "search_images", {"q": query.q, "lim": fetch, "off": offset}
            ).execute()
        elif isinstance(query, SimilarTo):
            response = await self._client.rpc(
                "similar_images", {"target_id": query.image_id, "lim": fetch, "off": offset}
            ).execute()
        else:
            response = await self._client.rpc(
                "images_by_color", {"hex": query.hex, "lim": fetch, "off": offset}
            ).execute()
        rows: list[dict[str, Any]] = response.data
        next_offset = offset + limit if len(rows) > limit else None
        return Page(items=[_row_to_record(row) for row in rows[:limit]], next_offset=next_offset)

    async def delete(self, image_id: int) -> ImageRecord | None:
        """Delete the image and, through the FK cascade, its metadata."""
        record = await self.get(image_id)
        if record is None:
            return None
        response = await self._client.table(_IMAGES).delete().eq("id", image_id).execute()
        # Empty when the row was deleted by someone else between the read and the delete.
        return record if response.data else None

    @_retry_idempotent("count_uploaded_since")
    async def count_uploaded_since(self, since: datetime) -> int:
        """Count the caller's uploads at or after `since`, including images deleted since.

        It counts `upload_log`, where a trigger adds a row for every new image and nothing removes
        one, so deleting an image doesn't free its slot. A HEAD request: PostgREST sends the count
        in `Content-Range`, so no rows are transferred. RLS limits the count to the caller's rows.
        """
        response = (
            await self._client.table(_UPLOAD_LOG)
            .select("id", count=CountMethod.exact, head=True)
            .gte("uploaded_at", since.isoformat())
            .retry(False)
            .execute()
        )
        return response.count or 0

    @_retry_idempotent("request_reanalysis")
    async def request_reanalysis(self, image_id: int) -> bool:
        """Re-queue the image's analysis. False when it is missing, not the caller's, or running."""
        response = await self._client.rpc("request_reanalysis", {"p_image_id": image_id}).execute()
        # The RPC returns a bare boolean. Compare strictly so a surprising payload can't pass.
        return response.data is True
