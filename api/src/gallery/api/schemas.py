"""Pydantic models for the HTTP API. The TypeScript types are generated from these."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from gallery.domain.models import (
    AnalysisStatus,
    GalleryShare,
    ImageRecord,
    ShareStatus,
    SiteSettings,
)


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
    shared_by: str | None = Field(
        default=None,
        description=(
            "The owner's e-mail address when the image is in a gallery shared with the caller. "
            "Null for the caller's own images. A shared image is read-only for the caller."
        ),
    )


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


# One bump per chunk of a drop; the web client sends at most this many at a time.
MAX_BUMP_IMAGES = 50

# Postgres bigint, which image ids are. A larger number would be a database error, not a miss.
ImageId = Annotated[int, Field(ge=1, le=2**63 - 1)]


class BumpRequest(BaseModel):
    image_ids: list[ImageId] = Field(
        min_length=1,
        max_length=MAX_BUMP_IMAGES,
        description=(
            "Ids of the caller's images to move to the top of the gallery. The order matters: "
            "the last id ends up on top."
        ),
    )


class BumpResponse(BaseModel):
    # Ids that are missing, or not the caller's, are left out.
    items: list[ImageOut] = Field(
        description="The images that moved, in the order of the request, with signed thumbnails."
    )


# Something@something.something, without spaces. Whether an account uses it is the database's
# call; this only keeps obvious typos from a round trip. 254 is the longest address SMTP allows.
EmailAddress = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
]


class ShareRequest(BaseModel):
    email: EmailAddress = Field(description="The address of the account to share the gallery with.")


class ShareOut(BaseModel):
    id: int
    email: str = Field(
        description=(
            "The other account's address: the recipient of an outgoing share, the owner of an "
            "incoming one."
        )
    )
    status: ShareStatus
    created_at: datetime
    accepted_at: datetime | None


class ShareList(BaseModel):
    outgoing: list[ShareOut] = Field(description="Whom the caller shares their gallery with.")
    incoming: list[ShareOut] = Field(
        description=(
            "Who shares their gallery with the caller. A pending one is a request to accept."
        )
    )


def to_share_out(share: GalleryShare) -> ShareOut:
    """Map a domain share to the API shape. The other account's id stays inside the API."""
    return ShareOut(
        id=share.id,
        email=share.email,
        status=share.status,
        created_at=share.created_at,
        accepted_at=share.accepted_at,
    )


class SignupStatus(BaseModel):
    allow_signups: bool = Field(description="Whether new accounts can be created right now.")


class AccountOut(BaseModel):
    is_admin: bool = Field(description="Whether the caller may open the admin settings.")


class SiteSettingsOut(BaseModel):
    allow_signups: bool = Field(description="Whether new accounts can be created.")
    updated_at: datetime = Field(description="When an admin last changed a setting.")


class SiteSettingsUpdate(BaseModel):
    allow_signups: bool = Field(description="Open (true) or close (false) sign-ups.")


def to_site_settings_out(settings: SiteSettings) -> SiteSettingsOut:
    return SiteSettingsOut(allow_signups=settings.allow_signups, updated_at=settings.updated_at)


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
        shared_by=record.shared_by,
    )
