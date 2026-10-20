"""Protocols that services depend on. Adapters implement them, so services never import SDKs."""

from collections.abc import Sequence
from typing import Protocol

from gallery.domain.models import (
    Analysis,
    AnalysisJob,
    GalleryQuery,
    ImageRecord,
    NewImage,
    Page,
    TagVerdict,
)


class ImageRepo(Protocol):
    # Also creates the pending metadata row.
    async def insert(self, image: NewImage) -> ImageRecord: ...

    async def get(self, image_id: int) -> ImageRecord | None: ...

    async def list(self, query: GalleryQuery, *, limit: int, offset: int) -> Page[ImageRecord]: ...

    # Returns the deleted record so the caller can remove its stored objects.
    async def delete(self, image_id: int) -> ImageRecord | None: ...

    async def request_reanalysis(self, image_id: int) -> bool: ...


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
    async def claim(self, limit: int) -> list[AnalysisJob]: ...

    async def complete(
        self,
        image_id: int,
        *,
        tags: Sequence[str],
        description: str,
        colors: Sequence[str],
        analyzer_id: str,
    ) -> None: ...

    async def fail(
        self,
        image_id: int,
        *,
        error: str,
        colors: Sequence[str],
        retryable: bool,
    ) -> None: ...


class AnalysisCache(Protocol):
    async def get(self, content_sha256: str, analyzer_id: str) -> Analysis | None: ...

    async def put(self, content_sha256: str, analyzer_id: str, analysis: Analysis) -> None: ...
