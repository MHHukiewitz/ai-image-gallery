"""TagVerifier adapters and the factory that picks one from settings."""

from gallery.adapters.verifiers.jev import JevTagVerifier
from gallery.config import Settings
from gallery.ports import TagVerifier

__all__ = ["JevTagVerifier", "build_tag_verifier"]


def build_tag_verifier(settings: Settings) -> TagVerifier | None:
    """Return the verifier that `settings.TAG_VERIFIER` names, or None when it is "none".

    Raises `ValueError` when the chosen verifier is missing a setting, so a misconfigured worker
    stops at startup instead of warning on every job.
    """
    match settings.TAG_VERIFIER:
        case "none":
            return None
        case "jev":
            if not settings.TYPESAFE_API_KEY:
                raise ValueError("TAG_VERIFIER=jev needs TYPESAFE_API_KEY")
            return JevTagVerifier(
                api_key=settings.TYPESAFE_API_KEY,
                model=settings.JEV_MODEL,
                timeout=settings.TAG_VERIFIER_TIMEOUT_SECONDS,
            )
