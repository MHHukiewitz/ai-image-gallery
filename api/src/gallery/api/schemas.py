"""Pydantic models for the HTTP API. The TypeScript types are generated from these."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

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


# Lowercase hex, as hashlib and the browser's Web Crypto (once hex-encoded) both produce it.
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

# One lookup per chunk of a drop; the web client sends at most this many at a time.
MAX_LOOKUP_HASHES = 50


class LookupRequest(BaseModel):
    content_sha256: list[Sha256] = Field(
        min_length=1,
        max_length=MAX_LOOKUP_HASHES,
        description="SHA-256 of the files' bytes, as 64 lowercase hex characters.",
    )


class LookupResponse(BaseModel):
    # Hashes the caller has no image for are left out.
    matches: dict[str, int] = Field(
        description="Maps each given hash that matches one of the caller's images to its id."
    )


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
