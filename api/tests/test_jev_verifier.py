"""Tests for the tag verifier settings, the Jev adapter and the factory that builds it.

No test here reaches the network: the adapter talks to a `httpx2.MockTransport`.
"""

import pytest
from pydantic import ValidationError

from gallery.config import Settings

# --- settings ---------------------------------------------------------------------------------


def test_the_verifier_is_off_by_default(settings: Settings) -> None:
    assert settings.TAG_VERIFIER == "none"
    assert settings.TYPESAFE_API_KEY is None
    assert settings.JEV_MODEL == "jev-1.14.0"
    assert settings.TAG_MIN_PROBABILITY == 0.5
    assert settings.TAG_VERIFIER_TIMEOUT_SECONDS == 5.0


def test_the_typesafe_key_is_hidden_from_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-typesafe")

    assert "test-typesafe" not in repr(Settings())


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TAG_VERIFIER", "gemini"),
        ("TAG_MIN_PROBABILITY", "1.5"),
        ("TAG_MIN_PROBABILITY", "-0.1"),
        ("TAG_VERIFIER_TIMEOUT_SECONDS", "0"),
    ],
)
def test_invalid_verifier_settings_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match=name):
        Settings()
