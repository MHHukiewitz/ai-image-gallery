"""ImageAnalyzer adapters and the factory that picks one from settings."""

from gallery.adapters.analyzers.anthropic import AnthropicAnalyzer
from gallery.adapters.analyzers.fake import FakeAnalyzer
from gallery.adapters.analyzers.openai_compat import OpenAICompatAnalyzer
from gallery.config import Settings
from gallery.ports import ImageAnalyzer

__all__ = ["AnthropicAnalyzer", "FakeAnalyzer", "OpenAICompatAnalyzer", "build_analyzer"]


def build_analyzer(settings: Settings) -> ImageAnalyzer:
    """Return the analyzer that `settings.ANALYZER` names.

    Raises `ValueError` when the chosen adapter is missing a setting, so a misconfigured worker
    stops at startup instead of failing every job.
    """
    match settings.ANALYZER:
        case "fake":
            return FakeAnalyzer()
        case "openai_compat":
            return _build_openai_compat(settings)
        case "anthropic":
            return _build_anthropic(settings)


def _build_anthropic(settings: Settings) -> AnthropicAnalyzer:
    if not settings.ANTHROPIC_API_KEY:
        raise ValueError("ANALYZER=anthropic needs ANTHROPIC_API_KEY")
    if not settings.AI_MODEL:
        raise ValueError("ANALYZER=anthropic needs AI_MODEL, for example claude-haiku-5-5")
    return AnthropicAnalyzer(
        api_key=settings.ANTHROPIC_API_KEY,
        model=settings.AI_MODEL,
        effort=settings.AI_EFFORT,
        timeout=settings.AI_TIMEOUT_SECONDS,
    )


def _build_openai_compat(settings: Settings) -> OpenAICompatAnalyzer:
    if not settings.AI_API_KEY:
        raise ValueError(
            "ANALYZER=openai_compat needs AI_API_KEY (any non-empty value for a local server)"
        )
    if not settings.AI_MODEL:
        raise ValueError("ANALYZER=openai_compat needs AI_MODEL, for example a model id")
    return OpenAICompatAnalyzer(
        base_url=settings.AI_BASE_URL,
        api_key=settings.AI_API_KEY,
        model=settings.AI_MODEL,
        timeout=settings.AI_TIMEOUT_SECONDS,
    )
