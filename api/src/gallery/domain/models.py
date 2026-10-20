"""Domain models passed between services, ports and adapters. Plain data, no I/O."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class AnalysisStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AnalysisUsage:
    """Cost and latency of the provider calls behind one analysis.

    It's absent on cache hits and for FakeAnalyzer.
    """

    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class Analysis:
    """What an ImageAnalyzer returns. The cache stores only tags and description."""

    tags: tuple[str, ...]
    description: str
    usage: AnalysisUsage | None = None


@dataclass(frozen=True, slots=True)
class NewImage:
    """Insert payload built by the upload service."""

    user_id: UUID
    filename: str
    original_path: str
    thumbnail_path: str
    mime_type: str
    size_bytes: int
    width: int
    height: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class ImageRecord:
    """One row of the `image_details` view, plus an optional search score."""

    id: int
    user_id: UUID
    filename: str
    original_path: str
    thumbnail_path: str
    mime_type: str
    size_bytes: int
    width: int
    height: int
    content_sha256: str
    uploaded_at: datetime
    status: AnalysisStatus
    tags: tuple[str, ...]
    description: str | None
    colors: tuple[str, ...]
    error: str | None
    score: float | None = None


@dataclass(frozen=True, slots=True)
class AnalysisJob:
    image_id: int
    user_id: UUID
    original_path: str
    content_sha256: str
    attempts: int


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: list[T]
    next_offset: int | None


# A gallery query has exactly one mode.
@dataclass(frozen=True, slots=True)
class AllImages:
    pass


@dataclass(frozen=True, slots=True)
class TextQuery:
    q: str


@dataclass(frozen=True, slots=True)
class SimilarTo:
    image_id: int


@dataclass(frozen=True, slots=True)
class ColorQuery:
    hex: str  # "#rrggbb", lowercase


GalleryQuery = AllImages | TextQuery | SimilarTo | ColorQuery
