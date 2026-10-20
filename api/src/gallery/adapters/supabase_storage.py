"""ObjectStore on Supabase Storage.

The client carries the user's JWT, so the bucket policies confine every call to the caller's own
`{user_id}/` folder.
"""

import logging
from collections.abc import Sequence

from gallery.adapters.supabase_client import AsyncClient

logger = logging.getLogger(__name__)


class SupabaseObjectStore:
    def __init__(self, client: AsyncClient, bucket: str) -> None:
        self._bucket = client.storage.from_(bucket)

    async def put(self, path: str, data: bytes, content_type: str) -> None:
        """Write a new object. The bucket has no update policy, so an existing key is an error."""
        await self._bucket.upload(path, data, {"content-type": content_type})

    async def get(self, path: str) -> bytes:
        return await self._bucket.download(path)

    async def delete(self, paths: Sequence[str]) -> None:
        """Remove the objects. A path that doesn't exist is not an error."""
        if paths:
            await self._bucket.remove(list(paths))

    async def signed_urls(self, paths: Sequence[str], ttl_seconds: int) -> dict[str, str]:
        """Sign all paths in one request.

        A path that can't be signed, such as a missing object, is left out of the result.
        """
        if not paths:
            return {}
        entries = await self._bucket.create_signed_urls(list(paths), ttl_seconds)
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
