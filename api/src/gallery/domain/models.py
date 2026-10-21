"""Domain models passed between services, ports and adapters. Plain data, no I/O."""

from dataclasses import dataclass, field
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
class TagVerdict:
    """What a TagVerifier says about the tags of one image.

    `probabilities` maps a tag to the chance, from 0 to 1, that it describes the image. A tag
    the verifier didn't score has no entry. Each merge is `(kept, dropped)`: two overlapping tags
    where `kept` is the better search keyword. `usage` is absent for verifiers that cost nothing.
    """

    probabilities: dict[str, float]
    merges: tuple[tuple[str, str], ...] = ()
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
class Job:
    """One job a worker claimed from the `jobs` queue.

    `kind` picks its handler. `image_id` and `user_id` are None for a kind that isn't about one
    image. `attempts` includes the claim that handed the job out.
    """

    id: int
    kind: str
    image_id: int | None
    user_id: UUID | None
    attempts: int


@dataclass(frozen=True, slots=True)
class AnalysisTarget:
    """The image of an `analyze_image` job, as the worker finds it after the claim.

    `status` is the image's analysis state. Done or failed means the analysis already ended
    outside this job, because the worker from before the jobs table finished it while the job
    waited.
    """

    image_id: int
    original_path: str
    # The hash of the file as uploaded. The API strips metadata from the stored original (#85), so
    # this often differs from the digest of the stored bytes, and a signed-in user can write it
    # through PostgREST, so it can be forged. Use it for logging only: anything security-relevant,
    # like the shared AI cache key, must hash the downloaded bytes instead.
    content_sha256: str
    status: AnalysisStatus
    error: str | None


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
    """Words to look for. `embedding` is the query's meaning as a vector, when one was made.

    With it the search also finds images by meaning, and without it, by words alone.
    """

    q: str
    embedding: tuple[float, ...] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class SimilarTo:
    image_id: int


@dataclass(frozen=True, slots=True)
class ColorQuery:
    hex: str  # "#rrggbb", lowercase


GalleryQuery = AllImages | TextQuery | SimilarTo | ColorQuery
