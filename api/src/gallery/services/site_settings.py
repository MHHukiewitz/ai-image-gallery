"""Site settings use cases: whether sign-ups are open, who is an admin, and the admin's switch.

The repo runs as the caller and the database decides who is an admin, so this layer adds no check
of its own: a non-admin's call fails in the database with ForbiddenError.
"""

import logging

from gallery.domain.models import SiteSettings
from gallery.ports import SiteSettingsRepo

logger = logging.getLogger(__name__)


class SiteSettingsService:
    def __init__(self, repo: SiteSettingsRepo) -> None:
        self._repo = repo

    async def signups_allowed(self) -> bool:
        """Whether new accounts can be created. Anyone may ask, signed in or not."""
        return await self._repo.signups_allowed()

    async def is_admin(self) -> bool:
        return await self._repo.caller_is_admin()

    async def settings(self) -> SiteSettings:
        """The settings, for an admin. Raises ForbiddenError for anyone else."""
        return await self._repo.get()

    async def set_allow_signups(self, allow: bool) -> SiteSettings:
        """Open or close sign-ups. Raises ForbiddenError unless the caller is an admin."""
        settings = await self._repo.set_allow_signups(allow)
        # Who may join the site is worth a line in the log whenever it changes.
        logger.info("changed the sign-up switch", extra={"allow_signups": settings.allow_signups})
        return settings
