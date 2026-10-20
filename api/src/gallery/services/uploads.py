"""Upload use case: validate, thumbnail, store and record one image, or bump a duplicate.

Ingest is synchronous, so the thumbnail is ready when the request returns. The AI analysis is not
part of it: the new row starts `pending` and the worker picks it up.

An image the user already has is not stored again. Uploading it moves the existing image to the top
of their gallery instead.
"""

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from uuid import UUID, uuid4

from gallery.config import get_settings
from gallery.domain.errors import NotFoundError
from gallery.domain.filenames import display_filename
from gallery.domain.models import ImageRecord, NewImage
from gallery.domain.thumbnails import make_thumbnail
from gallery.domain.validation import ImageInfo, inspect_image
from gallery.ports import ImageRepo, ObjectStore

logger = logging.getLogger(__name__)

_THUMBNAIL_FILENAME = "thumb.webp"
_THUMBNAIL_MIME_TYPE = "image/webp"


@dataclass(frozen=True, slots=True)
class UploadResult:
    """What an upload produced: the image, its signed thumbnail URL, and whether it is new.

    `created` is False when the user already had this image and it was bumped to the top.
    """

    record: ImageRecord
    thumbnail_url: str
    created: bool


@cache
def _shared_processing_slots() -> asyncio.Semaphore:
    """Return the process-wide cap on uploads being decoded at once.

    Every request builds its own UploadService, so the cap has to live here, not on the
    instance. It's created on first use: the settings can't be read at import time, and a
    semaphore belongs to the event loop that first waits on it.
    """
    return asyncio.Semaphore(get_settings().UPLOAD_PROCESSING_CONCURRENCY)


class UploadService:
    def __init__(
        self,
        repo: ImageRepo,
        store: ObjectStore,
        *,
        max_bytes: int,
        max_pixels: int,
        signed_url_ttl: int,
        processing_slots: asyncio.Semaphore | None = None,
    ) -> None:
        self._repo = repo
        self._store = store
        # Public, because the route reads one byte more than this from the request.
        self.max_bytes = max_bytes
        self._max_pixels = max_pixels
        self._signed_url_ttl = signed_url_ttl
        # Injectable so a test can bound a service on its own, without the shared one.
        self._processing_slots = (
            _shared_processing_slots() if processing_slots is None else processing_slots
        )

    async def upload(self, user_id: UUID, raw_filename: str | None, data: bytes) -> UploadResult:
        """Store one image for `user_id`, or bump it if they already have it.

        Raises InvalidImageError, before anything is stored, when the file isn't an acceptable
        JPEG or PNG. If a storage or database step fails, the objects already written are removed
        and the error is re-raised.

        A file whose hash matches one of the user's images writes nothing to Storage: that image
        is moved to the top of the gallery and returned with `created=False`. The hash is computed
        from the bytes here, never taken from the client, and RLS keeps the lookup inside the
        caller's images.

        Two identical uploads in flight at the same moment can both pass the lookup and both be
        stored. That is accepted: the web client collapses identical files within a drop, which
        covers the common case, and a unique constraint isn't possible while production already
        holds duplicates.

        Once the row is inserted the upload has succeeded. If the thumbnail then can't be
        signed, the URL is an empty string: failing the request would report an error for an
        image that was saved, and the user would upload it again.
        """
        # Decoding is CPU-bound, so it runs in a thread to keep other requests moving. A burst of
        # photos would otherwise decode side by side and exhaust the container's memory.
        async with self._processing_slots:
            info, thumbnail, content_sha256 = await asyncio.to_thread(self._process, data)

        existing = (await self._repo.find_by_sha256([content_sha256])).get(content_sha256)
        if existing is not None:
            bumped = await self._repo.bump(existing.id)
            if bumped is not None:
                return await self._result(bumped, created=False)
            # The image was deleted between the lookup and the bump, so there is nothing to move.
            # Store the file as a new image, as if the lookup had found nothing.

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

        return await self._result(record, created=True)

    async def bump(self, image_id: int) -> UploadResult:
        """Move the caller's image to the top of their gallery.

        Raises NotFoundError when the image is missing or belongs to someone else: the repo runs
        as the caller, so the two look the same.
        """
        record = await self._repo.bump(image_id)
        if record is None:
            raise NotFoundError("Image not found.")
        return await self._result(record, created=False)

    async def bump_many(self, image_ids: Sequence[int]) -> list[UploadResult]:
        """Move several of the caller's images to the top of their gallery in one go.

        The last id ends up on top, as if the images had been bumped one after another. Returns
        the images that moved, in the order given, each with a signed thumbnail URL; all the
        URLs are signed in one Storage call. An id that is missing or belongs to someone else is
        left out of the result, because the repo runs as the caller and the two look the same.
        """
        records = await self._repo.bump_many(image_ids)
        urls = await self._sign_thumbnails([record.thumbnail_path for record in records])
        return [
            UploadResult(record, urls.get(record.thumbnail_path, ""), created=False)
            for record in records
        ]

    async def find_duplicates(self, sha256s: Sequence[str]) -> dict[str, int]:
        """Map each hash the caller already has an image for to that image's id.

        Hashes with no match are left out. The web client uses this to bump instead of uploading.
        """
        matches = await self._repo.find_by_sha256(list(dict.fromkeys(sha256s)))
        return {sha256: record.id for sha256, record in matches.items()}

    async def _result(self, record: ImageRecord, *, created: bool) -> UploadResult:
        return UploadResult(record, await self._sign_thumbnail(record.thumbnail_path), created)

    def _process(self, data: bytes) -> tuple[ImageInfo, bytes, str]:
        info = inspect_image(data, max_bytes=self.max_bytes, max_pixels=self._max_pixels)
        return info, make_thumbnail(data), hashlib.sha256(data).hexdigest()

    async def _sign_thumbnail(self, thumbnail_path: str) -> str:
        """Return the signed thumbnail URL, or "" when it can't be signed.

        The grid asks for fresh URLs while any is empty, so a later request fills this one in.
        """
        urls = await self._sign_thumbnails([thumbnail_path])
        return urls.get(thumbnail_path, "")

    async def _sign_thumbnails(self, thumbnail_paths: Sequence[str]) -> dict[str, str]:
        """Sign the thumbnails in one Storage call. A path that can't be signed is left out.

        If Storage fails altogether, nothing is signed and the caller shows empty URLs, which the
        grid refreshes on its next request: the images themselves were saved or moved.
        """
        if not thumbnail_paths:
            return {}
        try:
            return await self._store.signed_urls(thumbnail_paths, self._signed_url_ttl)
        except Exception:
            logger.warning(
                "could not sign the thumbnails of uploaded images",
                extra={"thumbnail_paths": list(thumbnail_paths)},
                exc_info=True,
            )
            return {}

    async def _remove_objects(self, paths: list[str]) -> None:
        """Delete objects a failed upload left behind, without hiding the error that caused it."""
        try:
            await self._store.delete(paths)
        except Exception:
            # The original error is still on its way to the caller. Orphans only cost storage.
            logger.exception(
                "could not remove the objects of a failed upload", extra={"paths": paths}
            )
