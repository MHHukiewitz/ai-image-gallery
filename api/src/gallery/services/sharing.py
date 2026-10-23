"""Sharing use cases: share the caller's gallery by e-mail, and accept, decline or end shares.

The repo runs as the caller, so a share that doesn't exist and one between two other users look
the same here, and both raise NotFoundError.
"""

from gallery.domain.errors import NotFoundError
from gallery.domain.models import GalleryShare, ShareDirection
from gallery.ports import ShareRepo


class SharingService:
    def __init__(self, repo: ShareRepo) -> None:
        self._repo = repo

    async def list(self) -> tuple[list[GalleryShare], list[GalleryShare]]:
        """Return the caller's outgoing and incoming shares, each newest first."""
        shares = await self._repo.list()
        outgoing = [share for share in shares if share.direction is ShareDirection.OUTGOING]
        incoming = [share for share in shares if share.direction is ShareDirection.INCOMING]
        return outgoing, incoming

    async def share(self, email: str) -> GalleryShare:
        """Ask the account that uses `email` to accept the caller's gallery.

        The request stays pending until that account accepts it. Raises ShareRefusedError when
        no account uses the address, it is the caller's own, or the caller already shares with it.
        """
        return await self._repo.create(email.strip().lower())

    async def accept(self, share_id: int) -> None:
        """Accept a share made to the caller, which adds the owner's images to their gallery."""
        if not await self._repo.accept(share_id):
            raise NotFoundError("Share not found.")

    async def remove(self, share_id: int) -> None:
        """End a share from either side: the owner revokes it, the recipient declines or leaves."""
        if not await self._repo.delete(share_id):
            raise NotFoundError("Share not found.")
