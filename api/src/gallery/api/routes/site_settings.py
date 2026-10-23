"""Site settings routes: whether sign-ups are open, the caller's account, and the admin's switch."""

from typing import Annotated

from fastapi import APIRouter, Depends

from gallery.api.deps import get_public_site_settings_repo, get_site_settings_repo
from gallery.api.schemas import (
    AccountOut,
    ErrorResponse,
    SignupStatus,
    SiteSettingsOut,
    SiteSettingsUpdate,
    to_site_settings_out,
)
from gallery.ports import SiteSettingsRepo
from gallery.services.site_settings import SiteSettingsService

router = APIRouter()

_FORBIDDEN = {"model": ErrorResponse, "description": "The caller is not an admin."}


def get_site_settings_service(
    repo: Annotated[SiteSettingsRepo, Depends(get_site_settings_repo)],
) -> SiteSettingsService:
    return SiteSettingsService(repo)


def get_public_site_settings_service(
    repo: Annotated[SiteSettingsRepo, Depends(get_public_site_settings_repo)],
) -> SiteSettingsService:
    return SiteSettingsService(repo)


@router.get("/api/v1/signups", response_model=SignupStatus)
async def get_signup_status(
    service: Annotated[SiteSettingsService, Depends(get_public_site_settings_service)],
) -> SignupStatus:
    """Whether new accounts can be created. Needs no sign-in: the sign-up page asks first."""
    return SignupStatus(allow_signups=await service.signups_allowed())


@router.get("/api/v1/account", response_model=AccountOut)
async def get_account(
    service: Annotated[SiteSettingsService, Depends(get_site_settings_service)],
) -> AccountOut:
    """What the caller's account may do. The web shows the admin settings only to an admin."""
    return AccountOut(is_admin=await service.is_admin())


@router.get("/api/v1/admin/settings", response_model=SiteSettingsOut, responses={403: _FORBIDDEN})
async def get_site_settings(
    service: Annotated[SiteSettingsService, Depends(get_site_settings_service)],
) -> SiteSettingsOut:
    """The site-wide settings. Admins only."""
    return to_site_settings_out(await service.settings())


@router.put("/api/v1/admin/settings", response_model=SiteSettingsOut, responses={403: _FORBIDDEN})
async def update_site_settings(
    body: SiteSettingsUpdate,
    service: Annotated[SiteSettingsService, Depends(get_site_settings_service)],
) -> SiteSettingsOut:
    """Open or close sign-ups. Admins only. Existing users can sign in either way."""
    return to_site_settings_out(await service.set_allow_signups(body.allow_signups))
