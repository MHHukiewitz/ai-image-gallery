"""Tests for the tag verifier settings, the Jev adapter and the factory that builds it.

No test here reaches the network: the adapter talks to a `httpx2.MockTransport`.
"""

import asyncio
import io
import json
import re
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from PIL import Image
from pydantic import ValidationError
from typesafe_sdk import AsyncTypeSafeClient

from gallery.adapters.verifiers import JevTagVerifier, build_tag_verifier
from gallery.adapters.verifiers.jev import MIN_SIDE, fit_image
from gallery.config import Settings
from gallery.domain.errors import TagVerificationError

API_KEY = "test-key"
MODEL = "jev-1.14.0"
TAGS = ("house", "red house", "sun", "hill", "tree")

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


# --- test doubles -----------------------------------------------------------------------------


def image_bytes(
    size: tuple[int, int] = (640, 480), *, mode: str = "RGB", format: str = "PNG"
) -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, (200, 30, 30)).save(out, format=format)
    return out.getvalue()


def jev_reply(
    scores: list[float],
    choices: dict[int, tuple[str, str, str]] | None = None,
    *,
    input_tokens: int | None = 2764,
) -> dict[str, Any]:
    """A /v1/systemone body: one noul per score, and `choices[n] = (a, b, winner)` as dedupe<n>."""
    answers: dict[str, Any] = {
        f"tag{index}": {"type": "noul", "noul": score} for index, score in enumerate(scores)
    }
    for index, (first, second, winner) in (choices or {}).items():
        answers[f"dedupe{index}"] = {
            "type": "choice",
            "choice": winner,
            "confidence": 0.74,
            "probabilities": {first: 0.26 if winner == second else 0.74, second: 0.5},
        }
    usage: dict[str, int] = {"output_tokens": 13}
    if input_tokens is not None:
        usage["input_tokens"] = input_tokens
    return {"model": MODEL, "answers": answers, "usage": usage}


class Jev:
    """A stand-in for the Jev API. It records requests and answers each with `handler`."""

    def __init__(self, handler: Callable[[httpx2.Request], httpx2.Response] | None = None) -> None:
        self.requests: list[httpx2.Request] = []
        self._handler = handler or (
            lambda request: httpx2.Response(200, json=jev_reply([0.97] * 5))
        )

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self._handler(request)

    def verifier(self, *, timeout: float = 5.0) -> JevTagVerifier:
        client = AsyncTypeSafeClient(
            api_key=API_KEY, transport=httpx2.MockTransport(self._handle), compression=False
        )
        return JevTagVerifier(api_key=API_KEY, model=MODEL, timeout=timeout, client=client)

    def sent(self, request: httpx2.Request | None = None) -> tuple[dict[str, Any], bytes]:
        """The JSON `request` part and the bytes of the first image part of a request."""
        request = request or self.requests[0]
        boundary = request.headers["content-type"].split("boundary=")[1].encode()
        parts: dict[str, bytes] = {}
        for chunk in request.content.split(b"--" + boundary)[1:-1]:
            head, _, body = chunk.partition(b"\r\n\r\n")
            name = re.search(rb'name="([^"]+)"', head)
            assert name is not None
            parts[name.group(1).decode()] = body.removesuffix(b"\r\n")
        return json.loads(parts["request"]), parts["asset-0"]


def jev_that_replies(body: dict[str, Any]) -> Jev:
    return Jev(lambda request: httpx2.Response(200, json=body))


def jev_that_fails_with(status: int) -> Jev:
    return Jev(lambda request: httpx2.Response(status, json={"detail": "nope"}))


def size_of(jpeg: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(jpeg)) as image:
        return image.size


# --- the request ------------------------------------------------------------------------------


async def test_the_request_carries_the_image_the_opt_in_header_and_a_question_per_tag() -> None:
    jev = jev_that_replies(jev_reply([0.97] * 5, {0: ("house", "red house", "red house")}))

    await jev.verifier().verify(image_bytes(), "image/png", TAGS)

    (request,) = jev.requests
    assert request.method == "POST"
    assert request.url.path == "/v1/systemone"
    assert request.headers["TypeSafe-Danger-Zone-Images"] == "true"
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    body, image = jev.sent()
    assert body["model"] == MODEL
    # The photo is a reference in the state, and the bytes travel as a multipart asset.
    assert re.fullmatch(r"<image:[0-9a-f]{32}>", body["state"]["photo"])
    assert body["assets"] == {body["state"]["photo"]: {"type": "image", "part": "asset-0"}}
    assert image.startswith(b"\xff\xd8\xff")
    assert set(body["questions"]) == {"tag0", "tag1", "tag2", "tag3", "tag4", "dedupe0"}


async def test_each_tag_gets_a_yes_no_question_that_names_it() -> None:
    jev = jev_that_replies(jev_reply([0.97] * 5))

    await jev.verifier().verify(image_bytes(), "image/png", TAGS)

    questions = jev.sent()[0]["questions"]
    for index, tag in enumerate(TAGS):
        question = questions[f"tag{index}"]
        assert question["type"] == "noul"
        assert f"'{tag}'" in question["instructions"]


async def test_overlapping_tags_get_one_choice_question() -> None:
    jev = jev_that_replies(jev_reply([0.97] * 5))

    await jev.verifier().verify(image_bytes(), "image/png", TAGS)

    question = jev.sent()[0]["questions"]["dedupe0"]
    assert question["type"] == "choice"
    assert set(question["criteria"]) == {"house", "red house"}


async def test_at_most_three_choice_questions_are_asked() -> None:
    tags = ("a", "big a", "b", "big b", "c", "big c", "d", "big d")
    jev = jev_that_replies(jev_reply([0.97] * len(tags)))

    await jev.verifier().verify(image_bytes(), "image/png", tags)

    names = set(jev.sent()[0]["questions"])
    assert {name for name in names if name.startswith("dedupe")} == {
        "dedupe0",
        "dedupe1",
        "dedupe2",
    }
    assert len([name for name in names if name.startswith("tag")]) == len(tags)


async def test_a_repeated_tag_is_asked_once() -> None:
    jev = jev_that_replies(jev_reply([0.97] * 3))

    await jev.verifier().verify(image_bytes(), "image/png", ("sun", "sun", "hill", "tree"))

    assert set(jev.sent()[0]["questions"]) == {"tag0", "tag1", "tag2"}


async def test_no_tags_means_no_request() -> None:
    jev = Jev()

    verdict = await jev.verifier().verify(image_bytes(), "image/png", ())

    assert jev.requests == []
    assert verdict.probabilities == {}
    assert verdict.merges == ()


# --- the answers ------------------------------------------------------------------------------


async def test_probabilities_merges_and_usage_are_mapped() -> None:
    scores = [0.97, 0.98, 0.04, 0.5, 0.31]
    jev = jev_that_replies(jev_reply(scores, {0: ("house", "red house", "red house")}))

    verdict = await jev.verifier().verify(image_bytes(), "image/png", TAGS)

    assert verdict.probabilities == dict(zip(TAGS, scores, strict=True))
    assert verdict.merges == (("red house", "house"),)
    assert verdict.usage is not None
    assert verdict.usage.input_tokens == 2764
    assert verdict.usage.output_tokens == 13
    assert verdict.usage.cost_usd == pytest.approx(2764 * 0.042 / 1_000_000)
    assert verdict.usage.latency_ms >= 0


async def test_the_first_tag_of_a_pair_can_win_the_merge() -> None:
    jev = jev_that_replies(jev_reply([0.97] * 5, {0: ("house", "red house", "house")}))

    verdict = await jev.verifier().verify(image_bytes(), "image/png", TAGS)

    assert verdict.merges == (("house", "red house"),)


async def test_an_answer_naming_neither_tag_of_the_pair_is_ignored() -> None:
    jev = jev_that_replies(jev_reply([0.97] * 5, {0: ("house", "red house", "castle")}))

    verdict = await jev.verifier().verify(image_bytes(), "image/png", TAGS)

    assert verdict.merges == ()


async def test_a_tag_without_an_answer_has_no_probability() -> None:
    reply = jev_reply([0.97] * 5)
    del reply["answers"]["tag2"]
    jev = jev_that_replies(reply)

    verdict = await jev.verifier().verify(image_bytes(), "image/png", TAGS)

    assert "sun" not in verdict.probabilities
    assert len(verdict.probabilities) == 4


async def test_missing_token_counts_leave_the_cost_unknown() -> None:
    jev = jev_that_replies(jev_reply([0.97] * 5, input_tokens=None))

    verdict = await jev.verifier().verify(image_bytes(), "image/png", TAGS)

    assert verdict.usage is not None
    assert verdict.usage.input_tokens is None
    assert verdict.usage.cost_usd is None


# --- failures ---------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
async def test_an_error_status_raises_tag_verification_error_without_retrying(status: int) -> None:
    jev = jev_that_fails_with(status)

    with pytest.raises(TagVerificationError) as raised:
        await jev.verifier().verify(image_bytes(), "image/png", TAGS)

    assert str(status) in str(raised.value)
    assert API_KEY not in str(raised.value)
    # The SDK would retry a 429 or 5xx twice by default. A failing verifier must not hold a job up.
    assert len(jev.requests) == 1


async def test_a_transport_timeout_raises_tag_verification_error() -> None:
    def time_out(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("too slow", request=request)

    with pytest.raises(TagVerificationError):
        await Jev(time_out).verifier().verify(image_bytes(), "image/png", TAGS)


async def test_a_call_that_outlasts_the_timeout_raises_tag_verification_error() -> None:
    async def hang(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(5)
        return httpx2.Response(200, json=jev_reply([0.97] * 5))

    client = AsyncTypeSafeClient(
        api_key=API_KEY, transport=httpx2.MockTransport(hang), compression=False
    )
    verifier = JevTagVerifier(api_key=API_KEY, model=MODEL, timeout=0.05, client=client)

    with pytest.raises(TagVerificationError, match="0.05 seconds"):
        await verifier.verify(image_bytes(), "image/png", TAGS)


async def test_a_connection_failure_raises_tag_verification_error() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    with pytest.raises(TagVerificationError):
        await Jev(refuse).verifier().verify(image_bytes(), "image/png", TAGS)


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(200, content=b"not json"),
        httpx2.Response(200, json={"model": MODEL}),
        httpx2.Response(200, json={"model": MODEL, "answers": {"tag0": {"type": "noul"}}}),
    ],
)
async def test_an_unreadable_response_raises_tag_verification_error(
    response: httpx2.Response,
) -> None:
    jev = Jev(lambda request: response)

    with pytest.raises(TagVerificationError):
        await jev.verifier().verify(image_bytes(), "image/png", TAGS)


async def test_bytes_that_are_not_an_image_raise_tag_verification_error_before_any_request() -> (
    None
):
    jev = Jev()

    with pytest.raises(TagVerificationError):
        await jev.verifier().verify(b"this is not an image", "image/png", TAGS)

    assert jev.requests == []


# --- image preparation ------------------------------------------------------------------------


async def test_a_small_image_is_sent_enlarged() -> None:
    jev = jev_that_replies(jev_reply([0.97] * 5))

    await jev.verifier().verify(image_bytes((64, 64)), "image/png", TAGS)

    assert size_of(jev.sent()[1]) == (MIN_SIDE, MIN_SIDE)


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        # Scaled up until the short side reaches 280 px, keeping the aspect ratio.
        ((100, 50), (560, 280)),
        ((50, 100), (280, 560)),
        ((279, 400), (280, 401)),
        # Scaling to 280 px would push the long side over 1568 px, so it stops there and the
        # short side is padded.
        ((100, 1000), (280, 1568)),
        ((1000, 100), (1568, 280)),
        # Already in range: sent as normalized, not resized.
        ((280, 280), (280, 280)),
        ((640, 480), (640, 480)),
        # Normalizing caps the long side at 1024 px first.
        ((4000, 3000), (1024, 768)),
    ],
)
def test_fit_image_puts_both_sides_between_280_and_1568(
    size: tuple[int, int], expected: tuple[int, int]
) -> None:
    fitted = size_of(fit_image(image_bytes(size)))

    assert fitted == expected
    assert all(280 <= side <= 1568 for side in fitted)


def test_fit_image_returns_a_jpeg_and_flattens_transparency() -> None:
    fitted = fit_image(image_bytes((64, 64), mode="RGBA"))

    assert fitted.startswith(b"\xff\xd8\xff")
    with Image.open(io.BytesIO(fitted)) as image:
        assert image.mode == "RGB"


def test_fit_image_rejects_bytes_that_are_not_an_image() -> None:
    with pytest.raises(TagVerificationError):
        fit_image(b"this is not an image")


# --- the factory ------------------------------------------------------------------------------


def test_the_factory_builds_no_verifier_by_default(settings: Settings) -> None:
    assert build_tag_verifier(settings) is None


def test_the_factory_builds_a_jev_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAG_VERIFIER", "jev")
    monkeypatch.setenv("TYPESAFE_API_KEY", API_KEY)
    monkeypatch.setenv("JEV_MODEL", "jev-test")
    monkeypatch.setenv("TAG_VERIFIER_TIMEOUT_SECONDS", "2.5")

    verifier = build_tag_verifier(Settings())

    assert isinstance(verifier, JevTagVerifier)
    assert verifier._model == "jev-test"
    assert verifier._timeout == 2.5


def test_the_factory_refuses_jev_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAG_VERIFIER", "jev")

    with pytest.raises(ValueError, match="TYPESAFE_API_KEY"):
        build_tag_verifier(Settings())
