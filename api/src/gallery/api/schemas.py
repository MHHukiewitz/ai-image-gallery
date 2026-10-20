"""Pydantic models for the HTTP API. The TypeScript types are generated from these."""

from datetime import datetime

from pydantic import BaseModel

from gallery.domain.models import AnalysisStatus, ImageRecord


class AnalysisOut(BaseModel):
    status: AnalysisStatus
    tags: list[str]
    description: str | None
    colors: list[str]
    error: str | None


class ImageOut(BaseModel):
    id: int
    filename: str
    uploaded_at: datetime
    width: int
    height: int
    # Signed URLs; the caller chooses the TTL.
    thumbnail_url: str
    # Only the detail endpoint sets this.
    original_url: str | None = None
    analysis: AnalysisOut
    # Only the search modes set this.
    score: float | None = None


class ImagePage(BaseModel):
    items: list[ImageOut]
    next_offset: int | None


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


def to_image_out(
    record: ImageRecord, *, thumbnail_url: str, original_url: str | None = None
) -> ImageOut:
    """Map a domain record to the API shape.

    Storage paths and the owner's user_id stay inside the API. The caller signs the URLs.
    """
    return ImageOut(
        id=record.id,
        filename=record.filename,
        uploaded_at=record.uploaded_at,
        width=record.width,
        height=record.height,
        thumbnail_url=thumbnail_url,
        original_url=original_url,
        analysis=AnalysisOut(
            status=record.status,
            tags=list(record.tags),
            description=record.description,
            colors=list(record.colors),
            error=record.error,
        ),
        score=record.score,
    )
