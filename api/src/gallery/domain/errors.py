"""Domain errors. Services raise these, and the API maps them to HTTP responses."""

from typing import Literal


class GalleryError(Exception):
    """Base class for every error the domain raises on purpose."""


class NotFoundError(GalleryError):
    """The requested image doesn't exist."""


class InvalidImageError(GalleryError):
    """The upload failed validation. `code` says which check failed."""

    def __init__(
        self,
        code: Literal["unsupported_type", "too_large", "too_many_pixels", "corrupt"],
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AnalyzerError(GalleryError):
    """The analyzer provider failed. `retryable` says whether a later attempt may succeed."""

    def __init__(self, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


class TagVerificationError(GalleryError):
    """The tag verifier couldn't check the tags. The analyzer's tags are kept as they are."""


class AuthError(GalleryError):
    """The request has no valid Supabase session."""
