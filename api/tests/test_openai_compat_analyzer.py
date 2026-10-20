"""Tests for the OpenAI-compatible analyzer and the helpers it shares with other adapters.

The `openai` SDK 3.x talks through `httpx2`, which `respx` can't intercept. So the tests inject
an `httpx2.MockTransport` through the SDK client. Nothing here reaches the network.
"""

import asyncio
import base64
import io
import json
import logging
from typing import Any

import httpx2
import openai
import pytest
from openai import AsyncOpenAI
from PIL import Image

from gallery.adapters.analyzers import build_analyzer, openai_compat
from gallery.adapters.analyzers.images import (
    VISION_MAX_SIDE,
    image_data_url,
    normalize_image,
)
from gallery.adapters.analyzers.openai_compat import OpenAICompatAnalyzer
from gallery.adapters.analyzers.parsing import InvalidAnalysisError, parse_analysis
from gallery.adapters.analyzers.prompt import (
    PROMPT_VERSION,
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    USER_PROMPT,
    repair_prompt,
)
from gallery.config import Settings
from gallery.domain.errors import AnalyzerError

BASE_URL = "http://provider.test/v1"
TAGS = ["Golden Retriever", "dog", "park", "grass", "sunny", "playful"]
DESCRIPTION = "A dog runs across a sunny park."


def png_bytes(size: tuple[int, int] = (64, 48), color: str = "red") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


def reply(tags: list[str] = TAGS, description: str = DESCRIPTION) -> str:
    return json.dumps({"tags": tags, "description": description})


def completion(
    content: str | None,
    *,
    prompt_tokens: int | None = 100,
    completion_tokens: int | None = 20,
    cost: float | None = None,
) -> httpx2.Response:
    body: dict[str, Any] = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
    }
    if prompt_tokens is not None and completion_tokens is not None:
        body["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        if cost is not None:
            body["usage"]["cost"] = cost
    return httpx2.Response(200, json=body)


def failure(status_code: int) -> httpx2.Response:
    message = {"error": {"message": f"provider says {status_code}", "code": status_code}}
    return httpx2.Response(status_code, json=message)


class Provider:
    """A scripted API. Each request gets the next step; the last step repeats once spent."""

    def __init__(self, *steps: httpx2.Response | Exception) -> None:
        self._steps = list(steps)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        step = self._steps.pop(0) if len(self._steps) > 1 else self._steps[0]
        self.requests.append(request)
        if isinstance(step, Exception):
            raise step
        return step

    @property
    def calls(self) -> int:
        return len(self.requests)

    def body(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def make_client(provider: Provider, base_url: str = BASE_URL, **options: Any) -> AsyncOpenAI:
    http_client = openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(provider))
    return AsyncOpenAI(api_key="test-key", base_url=base_url, http_client=http_client, **options)


def make_analyzer(
    provider: Provider, base_url: str = BASE_URL, timeout: float = 30.0, **options: Any
) -> OpenAICompatAnalyzer:
    return OpenAICompatAnalyzer(
        base_url=base_url,
        api_key="test-key",
        model="test-model",
        timeout=timeout,
        client=make_client(provider, base_url, **options),
    )


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record backoff delays instead of waiting for them."""
    recorded: list[float] = []

    async def record(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(openai_compat, "sleep", record)
    return recorded


def sent_image(provider: Provider, index: int = 0) -> Image.Image:
    url = provider.body(index)["messages"][1]["content"][1]["image_url"]["url"]
    prefix = "data:image/jpeg;base64,"
    assert url.startswith(prefix)
    return Image.open(io.BytesIO(base64.b64decode(url[len(prefix) :])))


# --- happy path and request shape ---


async def test_returns_normalized_tags_and_description() -> None:
    provider = Provider(completion(reply()))

    analysis = await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert analysis.tags == ("golden retriever", "dog", "park", "grass", "sunny", "playful")
    assert analysis.description == DESCRIPTION
    assert provider.calls == 1


def test_analyzer_id_names_provider_model_and_prompt_version() -> None:
    analyzer = make_analyzer(Provider(completion(reply())))

    assert analyzer.analyzer_id == f"openai_compat:test-model:{PROMPT_VERSION}"
    assert analyzer.analyzer_id == "openai_compat:test-model:v2"


async def test_request_carries_prompt_schema_and_credentials() -> None:
    provider = Provider(completion(reply()))

    await make_analyzer(provider).analyze(png_bytes(), "image/png")

    request = provider.requests[0]
    body = provider.body()
    assert request.method == "POST"
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-key"
    assert body["model"] == "test-model"
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "image_analysis", "strict": True, "schema": RESPONSE_SCHEMA},
    }
    system, user = body["messages"]
    assert system == {"role": "system", "content": SYSTEM_PROMPT}
    assert user["role"] == "user"
    assert user["content"][0] == {"type": "text", "text": USER_PROMPT}
    assert user["content"][1]["type"] == "image_url"


async def test_sends_the_image_as_a_jpeg_data_url_of_at_most_1024_px() -> None:
    provider = Provider(completion(reply()))

    await make_analyzer(provider).analyze(png_bytes((3000, 2000)), "image/png")

    image = sent_image(provider)
    assert image.format == "JPEG"
    assert image.size == (1024, 683)
    assert max(image.size) <= VISION_MAX_SIDE


async def test_does_not_enlarge_small_images() -> None:
    provider = Provider(completion(reply()))

    await make_analyzer(provider).analyze(png_bytes((200, 100)), "image/png")

    assert sent_image(provider).size == (200, 100)


async def test_unreadable_image_fails_without_calling_the_provider() -> None:
    provider = Provider(completion(reply()))

    with pytest.raises(AnalyzerError) as raised:
        await make_analyzer(provider).analyze(b"not an image", "image/png")

    assert raised.value.retryable is False
    assert provider.calls == 0


async def test_client_timeout_applies_to_requests() -> None:
    provider = Provider(completion(reply()))

    await make_analyzer(provider, timeout=12.0).analyze(png_bytes(), "image/png")

    assert provider.requests[0].extensions["timeout"]["read"] == 12.0


async def test_openrouter_is_asked_to_report_cost() -> None:
    provider = Provider(completion(reply()))
    analyzer = make_analyzer(provider, base_url="https://openrouter.ai/api/v1")

    await analyzer.analyze(png_bytes(), "image/png")

    assert provider.body()["usage"] == {"include": True}


@pytest.mark.parametrize(
    "base_url",
    [BASE_URL, "https://api.openai.com/v1", "https://notopenrouter.ai.example.com/v1"],
)
async def test_other_providers_get_no_cost_field(base_url: str) -> None:
    provider = Provider(completion(reply()))

    await make_analyzer(provider, base_url=base_url).analyze(png_bytes(), "image/png")

    assert "usage" not in provider.body()


# --- transient failures ---


async def test_rate_limit_then_success_makes_two_calls(sleeps: list[float]) -> None:
    provider = Provider(failure(429), completion(reply()))

    analysis = await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert analysis.tags[0] == "golden retriever"
    assert provider.calls == 2
    assert sleeps == [0.5]


async def test_server_errors_three_times_raise_a_retryable_error(sleeps: list[float]) -> None:
    provider = Provider(failure(500), failure(502), failure(503))

    with pytest.raises(AnalyzerError) as raised:
        await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert raised.value.retryable is True
    assert raised.value.message == "AI service unavailable"
    assert provider.calls == 3
    assert sleeps == [0.5, 2.0]


@pytest.mark.parametrize(
    "transient",
    [
        httpx2.ReadTimeout("slow"),
        httpx2.ConnectError("refused"),
        failure(408),
    ],
)
async def test_timeouts_connection_errors_and_408_are_retried(
    transient: httpx2.Response | Exception, sleeps: list[float]
) -> None:
    provider = Provider(transient, completion(reply()))

    analysis = await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert analysis.description == DESCRIPTION
    assert provider.calls == 2
    assert sleeps == [0.5]


async def test_persistent_timeouts_raise_a_retryable_error() -> None:
    provider = Provider(httpx2.ReadTimeout("slow"))

    with pytest.raises(AnalyzerError) as raised:
        await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert raised.value.retryable is True
    assert provider.calls == 3


async def test_sdk_retries_stay_off_even_on_a_client_with_defaults() -> None:
    provider = Provider(failure(500))
    # The injected client keeps the SDK default of 2 retries. The analyzer must override it.
    analyzer = make_analyzer(provider)
    assert analyzer._client.max_retries == 0

    with pytest.raises(AnalyzerError):
        await analyzer.analyze(png_bytes(), "image/png")

    assert provider.calls == 3


# --- rejected requests ---


async def test_unauthorized_is_not_retryable(sleeps: list[float]) -> None:
    provider = Provider(failure(401))

    with pytest.raises(AnalyzerError) as raised:
        await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert raised.value.retryable is False
    assert raised.value.message == "AI service rejected the request"
    assert "401" not in raised.value.message
    assert provider.calls == 1
    assert sleeps == []


@pytest.mark.parametrize("status_code", [400, 402, 403, 404, 413, 422])
async def test_other_client_errors_are_not_retryable(status_code: int) -> None:
    provider = Provider(failure(status_code))

    with pytest.raises(AnalyzerError) as raised:
        await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert raised.value.retryable is False
    assert provider.calls == 1


# --- unusable replies ---


async def test_invalid_json_then_a_repaired_result() -> None:
    provider = Provider(completion("Sure! Here are some tags."), completion(reply()))

    analysis = await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert analysis.tags[0] == "golden retriever"
    assert provider.calls == 2
    messages = provider.body(1)["messages"]
    # The repair call replays the conversation, then adds the bad reply and what was wrong.
    assert messages[:2] == provider.body(0)["messages"]
    assert messages[2] == {"role": "assistant", "content": "Sure! Here are some tags."}
    assert messages[3]["role"] == "user"
    assert "not valid JSON" in messages[3]["content"]
    assert messages[3]["content"] == repair_prompt("the reply was not valid JSON (Expecting value)")
    assert sent_image(provider, 1).size == sent_image(provider, 0).size


async def test_invalid_json_twice_raises_a_retryable_error() -> None:
    provider = Provider(completion("nope"), completion("still nope"))

    with pytest.raises(AnalyzerError) as raised:
        await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert raised.value.retryable is True
    assert raised.value.message == "AI returned an invalid result"
    assert provider.calls == 2


async def test_three_tags_trigger_a_repair_attempt() -> None:
    short = reply(["dog", "park", "grass"])
    provider = Provider(completion(short), completion(reply()))

    analysis = await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert len(analysis.tags) == 6
    assert provider.calls == 2
    assert "only 3 usable tags" in provider.body(1)["messages"][3]["content"]


async def test_three_tags_twice_raise_a_retryable_error() -> None:
    provider = Provider(completion(reply(["dog", "park", "grass"])))

    with pytest.raises(AnalyzerError) as raised:
        await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert raised.value.retryable is True
    assert provider.calls == 2


async def test_duplicates_count_once_when_checking_the_minimum() -> None:
    padded = reply(["dog", "Dog", "DOG!", "park", "grass", "park"])
    provider = Provider(completion(padded), completion(reply()))

    await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert provider.calls == 2
    assert "only 3 usable tags" in provider.body(1)["messages"][3]["content"]


async def test_an_error_body_without_choices_is_treated_as_an_empty_reply() -> None:
    # OpenRouter can answer 200 with {"error": ...} when the upstream provider fails.
    upstream_error = httpx2.Response(200, json={"error": {"message": "upstream", "code": 502}})
    provider = Provider(upstream_error, completion(reply()))

    analysis = await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert analysis.description == DESCRIPTION
    assert provider.calls == 2
    assert provider.body(1)["messages"][2]["content"] == "(empty reply)"


async def test_a_reply_wrapped_in_a_code_fence_needs_no_repair() -> None:
    provider = Provider(completion(f"```json\n{reply()}\n```"))

    analysis = await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert analysis.description == DESCRIPTION
    assert provider.calls == 1


async def test_a_transient_failure_during_repair_is_retried_too(sleeps: list[float]) -> None:
    provider = Provider(completion("nope"), failure(503), completion(reply()))

    await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert provider.calls == 3
    assert sleeps == [0.5]


# --- usage ---


async def test_usage_maps_tokens_and_provider_cost() -> None:
    provider = Provider(completion(reply(), prompt_tokens=1400, completion_tokens=90, cost=0.00042))

    analysis = await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert analysis.usage is not None
    assert analysis.usage.input_tokens == 1400
    assert analysis.usage.output_tokens == 90
    assert analysis.usage.cost_usd == pytest.approx(0.00042)
    assert isinstance(analysis.usage.latency_ms, int)
    assert analysis.usage.latency_ms >= 0


async def test_cost_is_none_when_the_provider_does_not_report_it() -> None:
    provider = Provider(completion(reply(), prompt_tokens=10, completion_tokens=5))

    analysis = await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert analysis.usage is not None
    assert analysis.usage.cost_usd is None
    assert analysis.usage.input_tokens == 10


async def test_usage_tokens_are_none_when_the_provider_reports_no_usage() -> None:
    provider = Provider(completion(reply(), prompt_tokens=None, completion_tokens=None))

    analysis = await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert analysis.usage is not None
    assert analysis.usage.input_tokens is None
    assert analysis.usage.output_tokens is None
    assert analysis.usage.cost_usd is None


async def test_repair_attempt_sums_tokens_and_cost_of_both_calls() -> None:
    provider = Provider(
        completion("nope", prompt_tokens=1400, completion_tokens=10, cost=0.0004),
        completion(reply(), prompt_tokens=1500, completion_tokens=80, cost=0.0005),
    )

    analysis = await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert analysis.usage is not None
    assert analysis.usage.input_tokens == 2900
    assert analysis.usage.output_tokens == 90
    assert analysis.usage.cost_usd == pytest.approx(0.0009)


async def test_latency_covers_the_whole_call_including_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_sleep(seconds: float) -> None:
        await asyncio.sleep(0.05)

    monkeypatch.setattr(openai_compat, "sleep", slow_sleep)
    provider = Provider(failure(429), completion("nope"), completion(reply()))

    analysis = await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert analysis.usage is not None
    assert analysis.usage.latency_ms >= 50


# --- logging ---


async def test_logs_never_contain_the_key_or_image_data(caplog: pytest.LogCaptureFixture) -> None:
    provider = Provider(failure(503), completion("nope"), failure(401))

    with caplog.at_level(logging.DEBUG), pytest.raises(AnalyzerError):
        await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert caplog.records
    assert "test-key" not in caplog.text
    assert "base64" not in caplog.text
    levels = {record.levelno for record in caplog.records}
    assert logging.ERROR in levels  # a rejected key is a bug someone must see in Sentry


# --- shared helpers: image prep, parsing, prompt ---


def test_normalize_image_downscales_and_returns_jpeg() -> None:
    mime_type, data = normalize_image(png_bytes((2000, 3000)))

    image = Image.open(io.BytesIO(data))
    assert mime_type == "image/jpeg"
    assert image.format == "JPEG"
    assert image.size == (683, 1024)


def test_normalize_image_puts_transparent_areas_on_white() -> None:
    out = io.BytesIO()
    Image.new("RGBA", (20, 20), (0, 0, 0, 0)).save(out, format="PNG")

    _, data = normalize_image(out.getvalue())

    pixel = Image.open(io.BytesIO(data)).getpixel((10, 10))
    assert isinstance(pixel, tuple)
    assert all(channel >= 250 for channel in pixel)


def test_normalize_image_puts_palette_transparency_on_white() -> None:
    palette = Image.new("P", (20, 20), 0)
    palette.putpalette([0, 0, 0] * 256)
    out = io.BytesIO()
    palette.save(out, format="PNG", transparency=0)

    _, data = normalize_image(out.getvalue())

    pixel = Image.open(io.BytesIO(data)).getpixel((10, 10))
    assert isinstance(pixel, tuple)
    assert all(channel >= 250 for channel in pixel)


def test_normalize_image_applies_exif_orientation() -> None:
    exif = Image.Exif()
    exif[0x0112] = 6  # stored sideways; display rotated 90 degrees clockwise
    out = io.BytesIO()
    Image.new("RGB", (40, 20), "blue").save(out, format="JPEG", exif=exif)

    _, data = normalize_image(out.getvalue())

    assert Image.open(io.BytesIO(data)).size == (20, 40)


def test_normalize_image_rejects_bytes_that_are_not_an_image() -> None:
    with pytest.raises(AnalyzerError) as raised:
        normalize_image(b"junk")

    assert raised.value.retryable is False


def test_image_data_url_is_base64() -> None:
    url = image_data_url("image/jpeg", b"\xff\xd8abc")

    assert url == "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8abc").decode()


def test_parse_analysis_normalizes_what_the_model_returned() -> None:
    analysis = parse_analysis(
        json.dumps(
            {
                "tags": [" Beach ", "SAND", "sand!", "sea", "sky", "boat", "a b c d e"],
                "description": "Boats rest on a beach.  Waves roll in.",
            }
        )
    )

    assert analysis.tags == ("beach", "sand", "sea", "sky", "boat")
    assert analysis.description == "Boats rest on a beach."
    assert analysis.usage is None


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        ("", "not valid JSON"),
        ("[]", "not a JSON object"),
        ('"text"', "not a JSON object"),
        ('{"description": "x"}', '"tags" must be a list of strings'),
        ('{"tags": "dog", "description": "x"}', '"tags" must be a list of strings'),
        ('{"tags": [1, 2, 3, 4, 5], "description": "x"}', '"tags" must be a list of strings'),
        ('{"tags": ["a", "b", "c", "d", "e"]}', '"description" must be a string'),
        ('{"tags": ["a", "b", "c", "d", "e"], "description": "  "}', '"description" was empty'),
        ('{"tags": ["a", "b"], "description": "x"}', "only 2 usable tags"),
    ],
)
def test_parse_analysis_rejects_unusable_replies(raw: str, problem: str) -> None:
    with pytest.raises(InvalidAnalysisError, match=problem):
        parse_analysis(raw)


def test_response_schema_asks_for_five_to_ten_tags_and_a_description() -> None:
    tags = RESPONSE_SCHEMA["properties"]["tags"]

    assert (tags["minItems"], tags["maxItems"]) == (5, 10)
    assert tags["items"] == {"type": "string"}
    assert RESPONSE_SCHEMA["required"] == ["tags", "description"]
    assert RESPONSE_SCHEMA["additionalProperties"] is False


def test_prompt_states_the_rules_the_normalizers_enforce() -> None:
    for rule in ("lowercase", "one or two words", "one sentence", "20 words", "5 and 10"):
        assert rule in SYSTEM_PROMPT


def test_repair_prompt_includes_the_problem() -> None:
    assert "only 3 usable tags" in repair_prompt("only 3 usable tags remained")


# --- factory ---


def test_factory_builds_an_analyzer_from_ai_settings() -> None:
    analyzer = build_analyzer(
        Settings(
            ANALYZER="openai_compat",
            AI_BASE_URL="http://ollama.test:11434/v1",
            AI_API_KEY="test-key",
            AI_MODEL="llava",
            AI_TIMEOUT_SECONDS=7.5,
        )
    )

    assert isinstance(analyzer, OpenAICompatAnalyzer)
    assert analyzer.analyzer_id == "openai_compat:llava:v2"
    assert str(analyzer._client.base_url) == "http://ollama.test:11434/v1/"
    assert analyzer._client.timeout == 7.5
    assert analyzer._client.max_retries == 0


@pytest.mark.parametrize(
    ("missing", "overrides"),
    [("AI_API_KEY", {"AI_MODEL": "m"}), ("AI_MODEL", {"AI_API_KEY": "test-key"})],
)
def test_factory_names_the_missing_setting(missing: str, overrides: dict[str, str]) -> None:
    with pytest.raises(ValueError, match=missing):
        build_analyzer(Settings(ANALYZER="openai_compat", **overrides))
