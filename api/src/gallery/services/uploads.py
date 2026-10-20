"""Upload use case: validate, thumbnail, store and record one image.

Ingest is synchronous, so the thumbnail is ready when the request returns. The AI analysis is not
part of it: the new row starts `pending` and the worker picks it up.
"""

import asyncio
import hashlib
import logging
from uuid import UUID, uuid4

from gallery.domain.filenames import display_filename
from gallery.domain.models import ImageRecord, NewImage
from gallery.domain.thumbnails import make_thumbnail
from gallery.domain.validation import ImageInfo, inspect_image
from gallery.ports import ImageRepo, ObjectStore

logger = logging.getLogger(__name__)

_THUMBNAIL_FILENAME = "thumb.webp"
_THUMBNAIL_MIME_TYPE = "image/webp"


class UploadService:
    def __init__(
        self,
        repo: ImageRepo,
        store: ObjectStore,
        *,
        max_bytes: int,
        max_pixels: int,
        signed_url_ttl: int,
    ) -> None:
        self._repo = repo
        self._store = store
        # Public, because the route reads one byte more than this from the request.
        self.max_bytes = max_bytes
        self._max_pixels = max_pixels
        self._signed_url_ttl = signed_url_ttl

    async def upload(
        self, user_id: UUID, raw_filename: str | None, data: bytes
    ) -> tuple[ImageRecord, str]:
        """Store one image for `user_id` and return its record and a signed thumbnail URL.

        Raises InvalidImageError, before anything is stored, when the file isn't an acceptable
        JPEG or PNG. If a storage or database step fails, the objects already written are removed
        and the error is re-raised.
        """
        # Decoding is CPU-bound, so it runs in a thread to keep other requests moving.
        info, thumbnail, content_sha256 = await asyncio.to_thread(self._process, data)

        # The bucket policy and the database CHECK both require this {user_id}/ prefix.
        folder = f"{user_id}/{uuid4()}"
        original_path = f"{folder}/original.{info.extension}"
        thumbnail_path = f"{folder}/{_THUMBNAIL_FILENAME}"
        try:
            await self._store.put(original_path, data, info.mime_type)
            await self._store.put(thumbnail_path, thumbnail, _THUMBNAIL_MIME_TYPE)
            record = await self._repo.insert(
                NewImage(
                    user_id=user_id,
                    filename=display_filename(raw_filename),
                    original_path=original_path,
                    thumbnail_path=thumbnail_path,
                    mime_type=info.mime_type,
                    size_bytes=len(data),
                    width=info.width,
                    height=info.height,
                    content_sha256=content_sha256,
                )
            )
        except Exception:
            await self._remove_objects([original_path, thumbnail_path])
            raise

        urls = await self._store.signed_urls([thumbnail_path], self._signed_url_ttl)
        return record, urls[thumbnail_path]

    def _process(self, data: bytes) -> tuple[ImageInfo, bytes, str]:
        info = inspect_image(data, max_bytes=self.max_bytes, max_pixels=self._max_pixels)
        return info, make_thumbnail(data), hashlib.sha256(data).hexdigest()

    async def _remove_objects(self, paths: list[str]) -> None:
        """Delete objects a failed upload left behind, without hiding the error that caused it."""
        try:
            await self._store.delete(paths)
        except Exception:
            # The original error is still on its way to the caller. Orphans only cost storage.
            logger.exception(
                "could not remove the objects of a failed upload", extra={"paths": paths}
            )
