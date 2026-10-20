"""ImageRepo on PostgREST.

The client carries the user's JWT, so RLS decides which rows exist for the caller. Another
user's image reads as missing, never as forbidden.
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from gallery.adapters.supabase_client import AsyncClient
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
    def __init__(self, client: AsyncClient) -> None:
        self._client = client

    async def insert(self, image: NewImage) -> ImageRecord:
        """Insert the image and its pending metadata, then read the record back."""
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
                }
            )
            .execute()
        )
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

    async def get(self, image_id: int) -> ImageRecord | None:
        response = (
            await self._client.table(_DETAILS).select("*").eq("id", image_id).limit(1).execute()
        )
        return _row_to_record(response.data[0]) if response.data else None

    async def find_by_sha256(self, sha256s: Sequence[str]) -> dict[str, ImageRecord]:
        """Find the caller's images by content hash, in one query.

        RLS limits the view to the caller's rows, so another user's copy of the same file is not
        a match. Rows come back newest upload first, and the first row per hash is kept.
        """
        if not sha256s:
            return {}
        response = (
            await self._client.table(_DETAILS)
            .select("*")
            .in_("content_sha256", list(sha256s))
            .order("last_uploaded_at", desc=True)
            .order("id", desc=True)
            .execute()
        )
        matches: dict[str, ImageRecord] = {}
        for row in response.data:
            matches.setdefault(row["content_sha256"], _row_to_record(row))
        return matches

    async def bump(self, image_id: int) -> ImageRecord | None:
        """Move the image to the top of the gallery. None when it is missing or not the caller's."""
        response = await self._client.rpc("bump_image", {"p_image_id": image_id}).execute()
        # The RPC returns a bare boolean. Compare strictly so a surprising payload can't pass.
        if response.data is not True:
            return None
        return await self.get(image_id)

    async def list(self, query: GalleryQuery, *, limit: int, offset: int) -> Page[ImageRecord]:
        """Return one page: most recently uploaded first for `AllImages`, best match for a search.

        The search functions return the `image_details` columns (all but `last_uploaded_at`) plus
        `score`, so their rows map like the view's. They come back in rank order, and this method
        never re-sorts them.
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
                .execute()
            )
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

    async def request_reanalysis(self, image_id: int) -> bool:
        """Re-queue the image's analysis. False when it is missing, not the caller's, or running."""
        response = await self._client.rpc("request_reanalysis", {"p_image_id": image_id}).execute()
        # The RPC returns a bare boolean. Compare strictly so a surprising payload can't pass.
        return response.data is True
