"""Upload route: one image per request."""

from typing import Annotated

from fastapi import APIRouter, Depends, UploadFile

from gallery.api.deps import (
    CurrentUser,
    get_current_user,
    get_image_repo,
    get_object_store,
    get_settings,
)
from gallery.api.schemas import ErrorResponse, ImageOut, to_image_out
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
    )


@router.post(
    "/api/v1/images",
    status_code=201,
    response_model=ImageOut,
    responses={
        413: {"model": ErrorResponse, "description": "The file is larger than MAX_UPLOAD_MB."},
        415: {"model": ErrorResponse, "description": "The file is not a JPEG or PNG."},
    },
)
async def upload_image(
    file: UploadFile,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    service: Annotated[UploadService, Depends(get_upload_service)],
) -> ImageOut:
    # One byte more than the limit is enough for the service to see the file is too large, and
    # it keeps a huge upload from being read into memory.
    data = await file.read(service.max_bytes + 1)
    record, thumbnail_url = await service.upload(user.id, file.filename, data)
    return to_image_out(record, thumbnail_url=thumbnail_url)
