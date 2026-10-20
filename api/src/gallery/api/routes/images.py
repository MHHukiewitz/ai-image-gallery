"""Gallery routes: list, detail, delete and reanalyze. Stubs until the gallery service lands."""

from fastapi import APIRouter, HTTPException, Query
from fastapi.exceptions import RequestValidationError

from gallery.api.schemas import ErrorResponse, ImageOut, ImagePage

router = APIRouter()


@router.get(
    "/api/v1/images",
    response_model=ImagePage,
    responses={
        422: {
            "model": ErrorResponse,
            "description": "Invalid parameter, or more than one of q, similar_to, color set.",
        },
    },
)
async def list_images(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    q: str | None = Query(None, min_length=1, max_length=200),
    similar_to: int | None = None,
    color: str | None = Query(None, pattern=r"^#[0-9a-fA-F]{6}$"),
) -> ImagePage:
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
    raise HTTPException(status_code=501)


@router.get("/api/v1/images/{image_id}", response_model=ImageOut)
async def get_image(image_id: int) -> ImageOut:
    raise HTTPException(status_code=501)


@router.delete("/api/v1/images/{image_id}", status_code=204)
async def delete_image(image_id: int) -> None:
    raise HTTPException(status_code=501)


@router.post("/api/v1/images/{image_id}/reanalyze", status_code=202)
async def reanalyze_image(image_id: int) -> None:
    raise HTTPException(status_code=501)
