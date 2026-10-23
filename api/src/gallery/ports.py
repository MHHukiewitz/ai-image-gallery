"""Protocols that services depend on. Adapters implement them, so services never import SDKs."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from gallery.domain.models import (
    Analysis,
    AnalysisTarget,
    GalleryQuery,
    ImageRecord,
    Job,
    NewImage,
    Page,
    TagVerdict,
)


class ImageRepo(Protocol):
    # Also creates the pending metadata row.
    async def insert(self, image: NewImage) -> ImageRecord: ...

    async def get(self, image_id: int) -> ImageRecord | None: ...

    # Maps each given hash to `user_id`'s image whose content_sha256 (the uploaded file) or
    # stored_sha256 (the stored file, without metadata) equals it. If several images match (older
    # uploads can), the most recently uploaded one is returned. Only the user's own images count:
    # an image in a gallery shared with them is not theirs to bump.
    async def find_by_sha256(
        self, user_id: UUID, sha256s: Sequence[str]
    ) -> dict[str, ImageRecord]: ...

    # Moves the image to the top of the caller's gallery. None when it is missing or not theirs.
    async def bump(self, image_id: int) -> ImageRecord | None: ...

    # Moves several images to the top in one call, in the order given: the last id ends up on top.
    # Returns the records of the images that moved, in that order; an id that is missing or not the
    # caller's is left out.
    async def bump_many(self, image_ids: Sequence[int]) -> list[ImageRecord]: ...

    async def list(self, query: GalleryQuery, *, limit: int, offset: int) -> Page[ImageRecord]: ...

    # Returns the deleted record so the caller can remove its stored objects.
    async def delete(self, image_id: int) -> ImageRecord | None: ...

    async def request_reanalysis(self, image_id: int) -> bool: ...

    # Counts the caller's uploads at or after `since`, including images deleted since, so deleting
    # doesn't free quota. Only the number comes back, which is all the daily quota needs. A bump
    # stores no new image, so it doesn't count.
    async def count_uploaded_since(self, since: datetime) -> int: ...


class ObjectStore(Protocol):
    async def put(self, path: str, data: bytes, content_type: str) -> None: ...

    async def get(self, path: str) -> bytes: ...

    async def delete(self, paths: Sequence[str]) -> None: ...

    async def signed_urls(self, paths: Sequence[str], ttl_seconds: int) -> dict[str, str]: ...


class ImageAnalyzer(Protocol):
    # Format: "{provider}:{model}:{prompt_version}".
    @property
    def analyzer_id(self) -> str: ...

    # Raises AnalyzerError on failure.
    async def analyze(self, image: bytes, mime_type: str) -> Analysis: ...


class TagVerifier(Protocol):
    # Judges tags the analyzer already chose. It can only filter and merge them, never add one.
    # Raises TagVerificationError on failure.
    async def verify(self, image: bytes, mime_type: str, tags: Sequence[str]) -> TagVerdict: ...


class JobQueue(Protocol):
    # Takes up to `limit` due jobs of the given kinds, oldest first, and marks them 'processing'.
    # A job whose worker went quiet is taken over, and every claim counts as an attempt.
    async def claim(self, kinds: Sequence[str], limit: int) -> list[Job]: ...

    # A job that is gone (its image was deleted) is not an error, here and below.
    async def complete(self, job_id: int) -> None: ...

    # Records a failed attempt. The queue decides between a retry and the end.
    async def fail(self, job_id: int, *, error: str, retryable: bool) -> None: ...

    # The image of an analyze_image job. None when the image was deleted.
    async def analysis_target(self, job_id: int) -> AnalysisTarget | None: ...

    # Stores the analysis on the image and completes its job, in one transaction.
    async def complete_analysis(
        self,
        job_id: int,
        *,
        tags: Sequence[str],
        description: str,
        colors: Sequence[str],
        analyzer_id: str,
    ) -> None: ...

    # Records a failed analysis attempt, as `fail` does, and stores the colors either way.
    async def fail_analysis(
        self,
        job_id: int,
        *,
        error: str,
        colors: Sequence[str],
        retryable: bool,
    ) -> None: ...

    # Attaches the embedding of the image's final tags and description. Call it after
    # `complete_analysis`: completing an analysis drops an embedding that no longer matches the new
    # text.
    async def store_embedding(
        self, image_id: int, vector: Sequence[float], model_id: str
    ) -> None: ...


class TextEmbedder(Protocol):
    # Names the model, for example "BAAI/bge-small-en-v1.5". It is stored with every vector,
    # because vectors from different models can't be compared.
    @property
    def model_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    # One unit-length vector per text, in the same order. Runs the model off the event loop.
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class AnalysisCache(Protocol):
    async def get(self, content_sha256: str, analyzer_id: str) -> Analysis | None: ...

    async def put(self, content_sha256: str, analyzer_id: str, analysis: Analysis) -> None: ...
