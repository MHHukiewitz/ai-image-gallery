"""Gallery routes: list, detail, delete and reanalyze. Each one maps HTTP to one service call."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from fastapi.exceptions import RequestValidationError

from gallery.api.deps import get_image_repo, get_object_store, get_settings
from gallery.api.schemas import ErrorResponse, ImageOut, ImagePage, to_image_out
from gallery.config import Settings
from gallery.domain.models import AllImages, ColorQuery, GalleryQuery, SimilarTo, TextQuery
from gallery.ports import ImageRepo, ObjectStore
from gallery.services.gallery import GalleryService

router = APIRouter()

_NOT_FOUND = {"model": ErrorResponse, "description": "No image with this id."}


def get_gallery_service(
    images: Annotated[ImageRepo, Depends(get_image_repo)],
    objects: Annotated[ObjectStore, Depends(get_object_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> GalleryService:
    return GalleryService(images, objects, signed_url_ttl=settings.SIGNED_URL_TTL_SECONDS)


def _gallery_query(q: str | None, similar_to: int | None, color: str | None) -> GalleryQuery:
    """Map the search parameters to one query. Two at once are ambiguous, so they're refused."""
    given = [
        name
        for name, value in (("q", q), ("similar_to", similar_to), ("color", color))
        if value is not None
    ]
    if len(given) > 1:
        # Raised as a validation error so main.py's handler returns the one error shape.
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("query",),
                    "msg": f"only one of q, similar_to, color may be set; got {', '.join(given)}",
                    "input": None,
                }
            ]
        )
    if q is not None:
        return TextQuery(q.strip())
    if similar_to is not None:
        return SimilarTo(similar_to)
    if color is not None:
        return ColorQuery(color.lower())
    return AllImages()


@router.get(
    "/api/v1/images",
    response_model=ImagePage,
    responses={
        422: {
            "model": ErrorResponse,
            "description": "Invalid parameter, or more than one of q, similar_to, color set.",
        },
        501: {"model": ErrorResponse, "description": "The search mode isn't built yet."},
    },
)
async def list_images(
    service: Annotated[GalleryService, Depends(get_gallery_service)],
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    q: str | None = Query(None, min_length=1, max_length=200),
    similar_to: int | None = None,
    color: str | None = Query(None, pattern=r"^#[0-9a-fA-F]{6}$"),
) -> ImagePage:
    """List the caller's images, newest first. Search with at most one of q, similar_to, color."""
    query = _gallery_query(q, similar_to, color)
    page, thumbnail_urls = await service.list(query, limit=limit, offset=offset)
    return ImagePage(
        items=[
            # A missing key means the store couldn't sign that thumbnail. Its URL is left empty.
            to_image_out(record, thumbnail_url=thumbnail_urls.get(record.thumbnail_path, ""))
            for record in page.items
        ],
        next_offset=page.next_offset,
    )


@router.get("/api/v1/images/{image_id}", response_model=ImageOut, responses={404: _NOT_FOUND})
async def get_image(
    image_id: int, service: Annotated[GalleryService, Depends(get_gallery_service)]
) -> ImageOut:
    """Return one image with its signed thumbnail and original URLs."""
    record, thumbnail_url, original_url = await service.get(image_id)
    return to_image_out(record, thumbnail_url=thumbnail_url, original_url=original_url)


@router.delete(
    "/api/v1/images/{image_id}",
    status_code=204,
    response_class=Response,
    responses={404: _NOT_FOUND},
)
async def delete_image(
    image_id: int, service: Annotated[GalleryService, Depends(get_gallery_service)]
) -> None:
    """Remove the image and its stored files."""
    await service.delete(image_id)


@router.post(
    "/api/v1/images/{image_id}/reanalyze",
    status_code=202,
    response_class=Response,
    responses={
        404: {
            "model": ErrorResponse,
            "description": "No image with this id, or its analysis is already running.",
        },
    },
)
async def reanalyze_image(
    image_id: int, service: Annotated[GalleryService, Depends(get_gallery_service)]
) -> None:
    """Queue a fresh analysis of the image."""
    await service.reanalyze(image_id)
