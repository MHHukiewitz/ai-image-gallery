import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from gallery.config import Settings, get_settings

ENV_EXAMPLE = Path(__file__).parents[2] / ".env.example"

EXPECTED_SETTINGS = {
    "SUPABASE_URL",
    "SUPABASE_PUBLISHABLE_KEY",
    "SUPABASE_SECRET_KEY",
    "SUPABASE_JWKS_URL",
    "SUPABASE_JWT_SECRET",
    "STORAGE_BUCKET",
    "SIGNED_URL_TTL_SECONDS",
    "MAX_UPLOAD_MB",
    "MAX_IMAGE_PIXELS",
    "ANALYZER",
    "ANTHROPIC_API_KEY",
    "AI_EFFORT",
    "AI_BASE_URL",
    "AI_API_KEY",
    "AI_MODEL",
    "AI_TIMEOUT_SECONDS",
    "WORKER_CONCURRENCY",
    "WORKER_POLL_MIN_SECONDS",
    "WORKER_POLL_MAX_SECONDS",
    "JOB_MAX_ATTEMPTS",
    "JOB_STALE_AFTER_SECONDS",
    "LOG_LEVEL",
    "SENTRY_DSN",
    "SENTRY_ENVIRONMENT",
    "SENTRY_TRACES_SAMPLE_RATE",
    "RELEASE",
}


def test_declares_exactly_the_documented_settings() -> None:
    assert set(Settings.model_fields) == EXPECTED_SETTINGS


def test_defaults_load_with_only_required_env_vars(settings: Settings) -> None:
    assert settings.SUPABASE_URL == "http://localhost:54321"
    assert settings.SUPABASE_PUBLISHABLE_KEY == "test-key"
    assert settings.SUPABASE_SECRET_KEY is None
    assert settings.SUPABASE_JWT_SECRET is None
    assert settings.STORAGE_BUCKET == "images"
    assert settings.SIGNED_URL_TTL_SECONDS == 3600
    assert settings.MAX_UPLOAD_MB == 10
    assert settings.MAX_IMAGE_PIXELS == 40_000_000
    assert settings.ANALYZER == "fake"
    assert settings.ANTHROPIC_API_KEY is None
    assert settings.AI_EFFORT == "low"
    assert settings.AI_BASE_URL == "https://openrouter.ai/api/v1"
    assert settings.AI_API_KEY is None
    assert settings.AI_MODEL == ""
    assert settings.AI_TIMEOUT_SECONDS == 30.0
    assert settings.WORKER_CONCURRENCY == 4
    assert settings.WORKER_POLL_MIN_SECONDS == 1.0
    assert settings.WORKER_POLL_MAX_SECONDS == 10.0
    assert settings.JOB_MAX_ATTEMPTS == 3
    assert settings.JOB_STALE_AFTER_SECONDS == 300
    assert settings.LOG_LEVEL == "INFO"
    assert settings.SENTRY_DSN is None
    assert settings.SENTRY_ENVIRONMENT == "development"
    assert settings.SENTRY_TRACES_SAMPLE_RATE == 0.0
    assert settings.RELEASE is None


def test_jwks_url_is_derived_from_supabase_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://abc.supabase.co/")

    assert Settings().SUPABASE_JWKS_URL == "https://abc.supabase.co/auth/v1/.well-known/jwks.json"


def test_explicit_jwks_url_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_JWKS_URL", "https://keys.example.test/jwks.json")

    assert Settings().SUPABASE_JWKS_URL == "https://keys.example.test/jwks.json"


@pytest.mark.parametrize("missing", ["SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY"])
def test_required_settings_must_be_set(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    monkeypatch.delenv(missing)

    with pytest.raises(ValidationError, match=missing):
        Settings()


@pytest.mark.parametrize(
    ("name", "value"),
    [("ANALYZER", "gemini"), ("AI_EFFORT", "max"), ("MAX_UPLOAD_MB", "lots")],
)
def test_invalid_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match=name):
        Settings()


def test_empty_env_values_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """`cp .env.example .env` leaves optional keys blank; defaults must still apply."""
    monkeypatch.setenv("SENTRY_DSN", "")
    monkeypatch.setenv("ANALYZER", "")

    settings = Settings()

    assert settings.SENTRY_DSN is None
    assert settings.ANALYZER == "fake"


def test_secrets_are_hidden_from_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic")
    monkeypatch.setenv("AI_API_KEY", "test-ai")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-jwt")

    text = repr(Settings())

    for secret in ("test-secret", "test-anthropic", "test-ai", "test-jwt"):
        assert secret not in text


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_env_example_matches_settings() -> None:
    keys = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", ENV_EXAMPLE.read_text(), flags=re.MULTILINE))
    api_keys = {key for key in keys if not key.startswith("VITE_")}

    assert api_keys == EXPECTED_SETTINGS


def test_env_example_loads_as_valid_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPABASE_URL")
    monkeypatch.delenv("SUPABASE_PUBLISHABLE_KEY")

    settings = Settings(_env_file=ENV_EXAMPLE)

    assert settings.SUPABASE_URL == "http://127.0.0.1:54321"
    assert settings.ANALYZER == "fake"
    assert settings.SENTRY_DSN is None
    assert settings.SUPABASE_SECRET_KEY is None
