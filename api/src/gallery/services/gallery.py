"""Gallery use cases: list, open, delete and re-analyze the caller's images.

The service depends on the ports only. The adapters run as the caller, so a missing image and
another user's image look the same here, and both raise NotFoundError.
"""

import logging
from collections.abc import Sequence

from gallery.domain.errors import NotFoundError
from gallery.domain.models import GalleryQuery, ImageRecord, Page, SimilarTo
from gallery.ports import ImageRepo, ObjectStore

logger = logging.getLogger(__name__)


class GalleryService:
    def __init__(self, repo: ImageRepo, store: ObjectStore, *, signed_url_ttl: int) -> None:
        self._repo = repo
        self._store = store
        self._signed_url_ttl = signed_url_ttl

    async def list(
        self, query: GalleryQuery, *, limit: int, offset: int
    ) -> tuple[Page[ImageRecord], dict[str, str]]:
        """Return one page and the signed thumbnail URLs for it, keyed by thumbnail path.

        All thumbnails are signed in one storage call, so a page costs one round trip however
        many images it holds. A thumbnail that can't be signed is missing from the dict. If
        signing fails altogether, such as when Storage is overloaded, the dict is empty and the
        page still comes back: the grid shows placeholders and asks again.

        A "similar to" search for an image the caller can't see raises NotFoundError. Without
        this check the search would return an empty page, which can't be told apart from an image
        with nothing similar to it.
        """
        if isinstance(query, SimilarTo) and await self._repo.get(query.image_id) is None:
            raise NotFoundError("Image not found.")
        page = await self._repo.list(query, limit=limit, offset=offset)
        thumbnail_urls = await self._sign([image.thumbnail_path for image in page.items])
        return page, thumbnail_urls

    async def get(self, image_id: int) -> tuple[ImageRecord, str, str]:
        """Return the image with its thumbnail and original URLs.

        A URL the store can't sign comes back as an empty string, and so do both when signing
        fails altogether. The storage adapter logs a single broken file, and the image's other
        details shouldn't be hidden by it.
        """
        record = await self._repo.get(image_id)
        if record is None:
            raise NotFoundError("Image not found.")
        urls = await self._sign([record.thumbnail_path, record.original_path])
        return record, urls.get(record.thumbnail_path, ""), urls.get(record.original_path, "")

    async def _sign(self, paths: Sequence[str]) -> dict[str, str]:
        """Sign `paths`, or return no URLs when the store fails.

        The rows are already loaded, and the caller can ask again, so a Storage outage shouldn't
        turn a readable gallery into a 500.
        """
        try:
            return await self._store.signed_urls(paths, self._signed_url_ttl)
        except Exception:
            logger.warning(
                "could not sign object urls, answering without them",
                extra={"path_count": len(paths)},
                exc_info=True,
            )
            return {}

    async def delete(self, image_id: int) -> None:
        """Remove the image's rows, then its stored objects.

        The rows go first. If the object delete fails, the caller gets a success for a deleted
        image and the files are orphaned, which an operator can clean up. The reverse order would
        leave a live row pointing at files that are gone.
        """
        record = await self._repo.delete(image_id)
        if record is None:
            raise NotFoundError("Image not found.")
        try:
            await self._store.delete([record.original_path, record.thumbnail_path])
        except Exception:
            logger.exception(
                "could not remove the stored objects of a deleted image",
                extra={"image_id": image_id},
            )

    async def reanalyze(self, image_id: int) -> None:
        """Queue a fresh analysis of the image.

        The RPC answers false for a missing image, one the caller doesn't own, and one that is
        being analyzed right now. All three raise NotFoundError here, as the API contract says.
        """
        if not await self._repo.request_reanalysis(image_id):
            raise NotFoundError("Image not found, or its analysis is already running.")
