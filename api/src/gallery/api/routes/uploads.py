"""Upload routes: one image per request, plus the duplicate lookup and the bumps."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, UploadFile

from gallery.api.deps import (
    CurrentUser,
    get_current_user,
    get_image_repo,
    get_object_store,
    get_settings,
)
from gallery.api.schemas import (
    BumpRequest,
    BumpResponse,
    ErrorResponse,
    ImageOut,
    LookupRequest,
    LookupResponse,
    to_image_out,
)
from gallery.config import Settings
from gallery.ports import ImageRepo, ObjectStore
from gallery.services.uploads import UploadService

router = APIRouter()


def get_upload_service(
    repo: Annotated[ImageRepo, Depends(get_image_repo)],
    store: Annotated[ObjectStore, Depends(get_object_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> UploadService:
    return UploadService(
        repo,
        store,
        max_bytes=settings.MAX_UPLOAD_MB * 1024 * 1024,
        max_pixels=settings.MAX_IMAGE_PIXELS,
        signed_url_ttl=settings.SIGNED_URL_TTL_SECONDS,
        daily_quota=settings.UPLOAD_DAILY_QUOTA,
        quota_exempt_emails=settings.upload_quota_exempt_emails,
    )


@router.post(
    "/api/v1/images",
    status_code=201,
    response_model=ImageOut,
    responses={
        200: {
            "model": ImageOut,
            "description": (
                "The caller already has this image. Nothing was stored; the existing image was "
                "moved to the top of their gallery and is returned."
            ),
        },
        413: {"model": ErrorResponse, "description": "The file is larger than MAX_UPLOAD_MB."},
        415: {"model": ErrorResponse, "description": "The file is not a JPEG or PNG."},
    },
)
async def upload_image(
    file: UploadFile,
    response: Response,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    service: Annotated[UploadService, Depends(get_upload_service)],
) -> ImageOut:
    """Store an image (201), or bump the caller's existing copy of it to the top (200)."""
    # One byte more than the limit is enough for the service to see the file is too large, and
    # it keeps a huge upload from being read into memory.
    data = await file.read(service.max_bytes + 1)
    result = await service.upload(user.id, file.filename, data, email=user.email)
    if not result.created:
        response.status_code = 200
    return to_image_out(result.record, thumbnail_url=result.thumbnail_url)


# These two don't need the user's id, but naming the dependency keeps the 401 from depending on
# how the repo is built.
@router.post(
    "/api/v1/images/lookup",
    response_model=LookupResponse,
    dependencies=[Depends(get_current_user)],
    responses={
        422: {
            "model": ErrorResponse,
            "description": "No hashes, more than 50, or a hash that isn't 64 lowercase hex digits.",
        },
    },
)
async def lookup_images(
    body: LookupRequest, service: Annotated[UploadService, Depends(get_upload_service)]
) -> LookupResponse:
    """Say which of these content hashes the caller already has an image for."""
    return LookupResponse(matches=await service.find_duplicates(body.content_sha256))


@router.post(
    "/api/v1/images/bump",
    response_model=BumpResponse,
    dependencies=[Depends(get_current_user)],
    responses={
        422: {
            "model": ErrorResponse,
            "description": "No ids, more than 50, or an id that isn't a positive integer.",
        },
    },
)
async def bump_images(
    body: BumpRequest, service: Annotated[UploadService, Depends(get_upload_service)]
) -> BumpResponse:
    """Move the caller's images to the top of their gallery, the last id on top.

    Ids that are missing or belong to someone else are left out of the answer, not an error.
    """
    results = await service.bump_many(body.image_ids)
    return BumpResponse(
        items=[to_image_out(r.record, thumbnail_url=r.thumbnail_url) for r in results]
    )


@router.post(
    "/api/v1/images/{image_id}/bump",
    response_model=ImageOut,
    dependencies=[Depends(get_current_user)],
    responses={404: {"model": ErrorResponse, "description": "No image with this id."}},
)
async def bump_image(
    image_id: int, service: Annotated[UploadService, Depends(get_upload_service)]
) -> ImageOut:
    """Move the image to the top of the caller's gallery, as if it had just been uploaded."""
    result = await service.bump(image_id)
    return to_image_out(result.record, thumbnail_url=result.thumbnail_url)
