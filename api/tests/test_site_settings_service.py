"""SiteSettingsService against an in-memory SiteSettingsRepo: no network and no Supabase."""

import logging
from datetime import UTC, datetime

import pytest

from gallery.domain.errors import ForbiddenError
from gallery.domain.models import SiteSettings
from gallery.services.site_settings import SiteSettingsService

CHANGED_AT = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)


class FakeSiteSettingsRepo:
    """Answers like the adapter: the admin-only calls raise ForbiddenError for anyone else."""

    def __init__(self, *, admin: bool, allow_signups: bool = True) -> None:
        self.admin = admin
        self.allow_signups = allow_signups

    async def signups_allowed(self) -> bool:
        return self.allow_signups

    async def caller_is_admin(self) -> bool:
        return self.admin

    async def get(self) -> SiteSettings:
        if not self.admin:
            raise ForbiddenError("Only an admin can do this.")
        return SiteSettings(allow_signups=self.allow_signups, updated_at=CHANGED_AT)

    async def set_allow_signups(self, allow: bool) -> SiteSettings:
        if not self.admin:
            raise ForbiddenError("Only an admin can do this.")
        self.allow_signups = allow
        return SiteSettings(allow_signups=allow, updated_at=CHANGED_AT)


async def test_anyone_learns_whether_signups_are_open() -> None:
    service = SiteSettingsService(FakeSiteSettingsRepo(admin=False, allow_signups=False))

    assert await service.signups_allowed() is False


@pytest.mark.parametrize("admin", [True, False])
async def test_is_admin_is_the_databases_answer(admin: bool) -> None:
    assert await SiteSettingsService(FakeSiteSettingsRepo(admin=admin)).is_admin() is admin


async def test_an_admin_closes_signups_and_the_change_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = FakeSiteSettingsRepo(admin=True)
    service = SiteSettingsService(repo)

    with caplog.at_level(logging.INFO, logger="gallery.services.site_settings"):
        settings = await service.set_allow_signups(False)

    assert settings == SiteSettings(allow_signups=False, updated_at=CHANGED_AT)
    assert await service.signups_allowed() is False
    assert await service.settings() == settings
    [record] = caplog.records
    assert record.getMessage() == "changed the sign-up switch"
    assert record.__dict__["allow_signups"] is False


async def test_anyone_else_can_neither_read_nor_change_the_settings() -> None:
    repo = FakeSiteSettingsRepo(admin=False)
    service = SiteSettingsService(repo)

    with pytest.raises(ForbiddenError):
        await service.settings()
    with pytest.raises(ForbiddenError):
        await service.set_allow_signups(False)
    assert repo.allow_signups is True
