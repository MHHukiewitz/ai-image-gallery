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


class QuotaExceededError(GalleryError):
    """The user has used up the daily upload quota. The API answers 429 with `Retry-After`.

    `retry_after_seconds` is an upper bound: the quota counts a rolling window, so a slot is free
    again no later than that many seconds from now.
    """

    def __init__(self, message: str, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after_seconds = retry_after_seconds


class AuthError(GalleryError):
    """The request has no valid Supabase session."""
