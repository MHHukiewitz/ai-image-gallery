"""Tests for the native Anthropic analyzer and its price table.

The `anthropic` SDK 1.x talks through `httpx2`, which `respx` can't intercept. So the tests inject
an `httpx2.MockTransport` through a real `AsyncAnthropic` client, which keeps the SDK's request
building, structured-output parsing and typed errors in play. Clients use `max_retries=0`, and
nothing here reaches the network.
"""

import base64
import io
import json
import logging
from datetime import date
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic import AsyncAnthropic
from PIL import Image

from gallery.adapters.analyzers import anthropic as anthropic_adapter
from gallery.adapters.analyzers import build_analyzer
from gallery.adapters.analyzers.anthropic import AnthropicAnalyzer
from gallery.adapters.analyzers.images import VISION_MAX_SIDE
from gallery.adapters.analyzers.pricing import PRICES, PRICING_URL, cost_usd
from gallery.adapters.analyzers.prompt import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    USER_PROMPT,
    repair_prompt,
)
from gallery.config import Settings
from gallery.domain.errors import AnalyzerError

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

TAGS = ["Golden Retriever", "dog", "park", "grass", "sunny", "playful"]
DESCRIPTION = "A dog runs across a sunny park."


def png_bytes(size: tuple[int, int] = (64, 48), color: str = "red") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


def reply(tags: list[str] = TAGS, description: str = DESCRIPTION) -> str:
    return json.dumps({"tags": tags, "description": description})


def message(
    content: list[dict[str, Any]],
    *,
    stop_reason: str = "end_turn",
    stop_details: dict[str, Any] | None = None,
    model: str = HAIKU,
    input_tokens: int = 1500,
    output_tokens: int = 120,
    iterations: list[dict[str, Any]] | None = None,
) -> httpx2.Response:
    usage: dict[str, Any] = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    if iterations is not None:
        usage["iterations"] = iterations
    return httpx2.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "stop_details": stop_details,
            "usage": usage,
        },
    )


def answer(text: str | None = None, **options: Any) -> httpx2.Response:
    """A normal reply whose text is `text`, or a valid analysis when that is omitted."""
    return message([{"type": "text", "text": reply() if text is None else text}], **options)


def failure(status_code: int, **headers: str) -> httpx2.Response:
    error_type = {400: "invalid_request_error", 401: "authentication_error"}.get(
        status_code, "api_error"
    )
    body = {
        "type": "error",
        "error": {"type": error_type, "message": f"provider says {status_code}"},
        "request_id": "req_test",
    }
    return httpx2.Response(status_code, json=body, headers={"request-id": "req_test", **headers})


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


def make_client(provider: Provider, **options: Any) -> AsyncAnthropic:
    options.setdefault("max_retries", 0)
    http_client = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(provider))
    return AsyncAnthropic(api_key="test-key", http_client=http_client, **options)


def make_analyzer(
    provider: Provider,
    model: str = HAIKU,
    effort: Any = "low",
    timeout: float = 30.0,
    **options: Any,
) -> AnthropicAnalyzer:
    return AnthropicAnalyzer(
        api_key="test-key",
        model=model,
        effort=effort,
        timeout=timeout,
        client=make_client(provider, **options),
    )


def sent_image(provider: Provider, index: int = 0) -> Image.Image:
    block = provider.body(index)["messages"][0]["content"][0]
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/jpeg"
    return Image.open(io.BytesIO(base64.b64decode(block["source"]["data"])))


# --- happy path and request shape ---


async def test_returns_normalized_tags_and_description() -> None:
    provider = Provider(
        answer(reply(["Golden Retriever", "dog", "DOG", "park!", "grass", "sunny"]))
    )

    analysis = await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert analysis.tags == ("golden retriever", "dog", "park", "grass", "sunny")
    assert analysis.description == DESCRIPTION
    assert provider.calls == 1


def test_analyzer_id_names_provider_model_effort_and_prompt_version() -> None:
    provider = Provider(answer())

    assert make_analyzer(provider).analyzer_id == f"anthropic:{HAIKU}:low:{PROMPT_VERSION}"
    assert (
        make_analyzer(provider, SONNET, "high").analyzer_id == "anthropic:claude-sonnet-5-5:high:v1"
    )


async def test_request_carries_prompt_effort_and_a_json_schema() -> None:
    provider = Provider(answer())

    await make_analyzer(provider).analyze(png_bytes(), "image/png")

    request = provider.requests[0]
    body = provider.body()
    assert request.method == "POST"
    assert request.url.path == "/v1/messages"
    assert request.headers["x-api-key"] == "test-key"
    assert body["model"] == HAIKU
    assert body["system"] == SYSTEM_PROMPT
    assert body["output_config"]["effort"] == "low"
    output_format = body["output_config"]["format"]
    assert output_format["type"] == "json_schema"
    assert set(output_format["schema"]["properties"]) == {"tags", "description"}
    assert output_format["schema"]["required"] == ["tags", "description"]


async def test_request_leaves_out_what_the_5_5_models_reject() -> None:
    provider = Provider(answer())

    await make_analyzer(provider).analyze(png_bytes(), "image/png")

    # `thinking: disabled` is a 400 on Sonnet 5.5 and Opus 5.5, sampling parameters are a 400 on
    # all three, and an assistant prefill is a 400 too.
    body = provider.body()
    for field in ("thinking", "temperature", "top_p", "top_k", "tool_choice"):
        assert field not in body
    assert body["messages"][-1]["role"] == "user"


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
async def test_effort_comes_from_the_constructor(effort: str) -> None:
    provider = Provider(answer())

    await make_analyzer(provider, effort=effort).analyze(png_bytes(), "image/png")

    assert provider.body()["output_config"]["effort"] == effort


async def test_sends_one_base64_jpeg_of_at_most_1024_px_before_the_instruction() -> None:
    provider = Provider(answer())

    await make_analyzer(provider).analyze(png_bytes((3000, 2000)), "image/png")

    (turn,) = provider.body()["messages"]
    assert turn["role"] == "user"
    assert [block["type"] for block in turn["content"]] == ["image", "text"]
    assert turn["content"][1]["text"] == USER_PROMPT
    image = sent_image(provider)
    assert image.format == "JPEG"
    assert image.size == (1024, 683)
    assert max(image.size) <= VISION_MAX_SIDE


async def test_does_not_enlarge_small_images() -> None:
    provider = Provider(answer())

    await make_analyzer(provider).analyze(png_bytes((64, 48)), "image/png")

    assert sent_image(provider).size == (64, 48)


async def test_haiku_uses_the_plain_endpoint_without_fallbacks() -> None:
    provider = Provider(answer())

    await make_analyzer(provider, HAIKU).analyze(png_bytes(), "image/png")

    request = provider.requests[0]
    assert "beta" not in request.url.params
    assert "anthropic-beta" not in request.headers
    assert "fallbacks" not in provider.body()


@pytest.mark.parametrize("model", [SONNET, OPUS])
async def test_sonnet_and_opus_opt_in_to_server_side_fallbacks(model: str) -> None:
    provider = Provider(answer(model=model))

    await make_analyzer(provider, model).analyze(png_bytes(), "image/png")

    request = provider.requests[0]
    body = provider.body()
    assert request.url.params["beta"] == "true"
    assert request.headers["anthropic-beta"] == FALLBACK_BETA
    assert body["fallbacks"] == "default"
    assert body["model"] == model
    assert body["output_config"]["effort"] == "low"
    assert "thinking" not in body


async def test_ignores_non_text_blocks_such_as_thinking() -> None:
    thinking = {"type": "thinking", "thinking": "", "signature": "test-signature"}
    provider = Provider(message([thinking, {"type": "text", "text": reply()}]))

    analysis = await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert analysis.description == DESCRIPTION


# --- errors ---

ANALYZER_LOGGER = "gallery.adapters.analyzers.anthropic"


def analyzer_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The records the analyzer logged, without the HTTP client's own request lines."""
    return [record for record in caplog.records if record.name == ANALYZER_LOGGER]


async def analyze_failing(provider: Provider, model: str = HAIKU) -> AnalyzerError:
    with pytest.raises(AnalyzerError) as raised:
        await make_analyzer(provider, model).analyze(png_bytes(), "image/png")
    return raised.value


@pytest.mark.parametrize("status_code", [429, 500, 502, 529])
async def test_rate_limits_and_server_errors_are_retryable(status_code: int) -> None:
    provider = Provider(failure(status_code))

    error = await analyze_failing(provider)

    assert error.retryable is True
    assert error.message == "AI service unavailable"
    assert provider.calls == 1


@pytest.mark.parametrize("status_code", [400, 401, 402, 403, 404, 413, 422])
async def test_rejected_requests_are_not_retryable(status_code: int) -> None:
    provider = Provider(failure(status_code))

    error = await analyze_failing(provider)

    assert error.retryable is False
    assert error.message == "AI service rejected the request"
    assert provider.calls == 1


@pytest.mark.parametrize(
    "problem",
    [httpx2.ConnectError("connection refused"), httpx2.ReadTimeout("timed out")],
    ids=["connection", "timeout"],
)
async def test_connection_errors_and_timeouts_are_retryable(problem: Exception) -> None:
    provider = Provider(problem)

    error = await analyze_failing(provider)

    assert error.retryable is True
    assert error.message == "AI service unavailable"


async def test_error_messages_hold_no_provider_details() -> None:
    for status_code in (401, 429):
        error = await analyze_failing(Provider(failure(status_code)))

        assert "provider says" not in error.message
        assert "test-key" not in error.message
        assert "anthropic" not in error.message.lower()


async def test_details_go_to_the_log_at_a_level_that_matches_the_cause(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    await analyze_failing(Provider(failure(401)))
    await analyze_failing(Provider(failure(429)))

    rejected, unavailable = analyzer_records(caplog)
    assert rejected.levelno == logging.ERROR
    assert vars(rejected)["status_code"] == 401
    assert vars(rejected)["request_id"] == "req_test"
    assert vars(rejected)["error_type"] == "AuthenticationError"
    assert "provider says 401" in vars(rejected)["provider_message"]
    assert unavailable.levelno == logging.WARNING
    assert vars(unavailable)["status_code"] == 429


async def test_logs_never_contain_the_key_or_image_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    image = png_bytes()

    await analyze_failing(Provider(failure(401)))
    await analyze_failing(Provider(httpx2.ConnectError("connection refused")))
    await make_analyzer(Provider(answer(reply(["a", "b"])), answer())).analyze(image, "image/png")

    assert caplog.records
    assert "test-key" not in caplog.text
    assert base64.b64encode(image).decode()[:40] not in caplog.text


async def test_leaves_retrying_to_the_sdk() -> None:
    provider = Provider(failure(429, **{"retry-after-ms": "1"}), answer())

    analysis = await make_analyzer(provider, max_retries=2).analyze(png_bytes(), "image/png")

    assert analysis.description == DESCRIPTION
    assert provider.calls == 2


def test_the_default_client_keeps_the_sdk_retries_and_applies_the_timeout() -> None:
    analyzer = AnthropicAnalyzer(api_key="test-key", model=HAIKU, effort="low", timeout=7.5)

    assert analyzer._client.max_retries == 2
    assert analyzer._client.timeout == 7.5


def test_an_injected_client_keeps_its_own_retries_and_gets_the_timeout() -> None:
    analyzer = make_analyzer(Provider(answer()), timeout=7.5, max_retries=0)

    assert analyzer._client.max_retries == 0
    assert analyzer._client.timeout == 7.5


# --- refusals ---

REFUSAL = {"type": "refusal", "category": "cyber", "explanation": None}


@pytest.mark.parametrize("model", [HAIKU, SONNET, OPUS])
async def test_a_refusal_is_permanent_and_not_repaired(model: str) -> None:
    provider = Provider(message([], stop_reason="refusal", stop_details=REFUSAL, model=model))

    error = await analyze_failing(provider, model)

    assert error.retryable is False
    assert error.message == "The AI declined to analyze this image"
    assert provider.calls == 1


async def test_a_refusal_without_details_is_still_a_refusal() -> None:
    provider = Provider(message([], stop_reason="refusal"))

    error = await analyze_failing(provider)

    assert error.retryable is False
    assert error.message == "The AI declined to analyze this image"


async def test_a_refusal_discards_partial_output() -> None:
    provider = Provider(answer(stop_reason="refusal", stop_details=REFUSAL))

    error = await analyze_failing(provider)

    assert error.message == "The AI declined to analyze this image"


async def test_a_refusal_on_the_repair_attempt_is_permanent() -> None:
    provider = Provider(
        answer(reply(["dog", "park", "grass"])),
        message([], stop_reason="refusal", stop_details=REFUSAL),
    )

    error = await analyze_failing(provider)

    assert error.retryable is False
    assert error.message == "The AI declined to analyze this image"
    assert provider.calls == 2


async def test_a_refusal_is_logged_with_its_category(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)

    await analyze_failing(Provider(message([], stop_reason="refusal", stop_details=REFUSAL)))

    (record,) = analyzer_records(caplog)
    assert record.levelno == logging.INFO
    assert vars(record)["category"] == "cyber"


# --- repair ---

THREE_TAGS = reply(["dog", "park", "grass"])
THINKING_ONLY = [{"type": "thinking", "thinking": "", "signature": "test-signature"}]


async def test_three_tags_trigger_one_repair_and_then_succeed() -> None:
    provider = Provider(answer(THREE_TAGS), answer())

    analysis = await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert analysis.tags == ("golden retriever", "dog", "park", "grass", "sunny", "playful")
    assert provider.calls == 2


async def test_the_repair_request_shows_the_model_its_reply_and_the_problem() -> None:
    provider = Provider(answer(THREE_TAGS), answer())

    await make_analyzer(provider).analyze(png_bytes(), "image/png")

    first, correction = provider.body(0)["messages"], provider.body(1)["messages"]
    assert correction[0] == first[0]
    assert correction[1] == {"role": "assistant", "content": THREE_TAGS}
    assert correction[2]["role"] == "user"
    assert "only 3 usable tags" in correction[2]["content"]
    assert correction[2]["content"].startswith(repair_prompt("only 3 usable tags")[:50])
    for body in (provider.body(0), provider.body(1)):
        assert body["output_config"]["effort"] == "low"
        assert "thinking" not in body


async def test_tags_that_collapse_to_fewer_than_five_trigger_a_repair() -> None:
    provider = Provider(answer(reply(["Dog", "dog", "DOG", "park", "sun", "---"])), answer())

    await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert provider.calls == 2


async def test_an_empty_description_triggers_a_repair() -> None:
    provider = Provider(answer(reply(TAGS, "   ")), answer())

    analysis = await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert analysis.description == DESCRIPTION
    assert provider.calls == 2


async def test_a_second_unusable_reply_is_a_retryable_error() -> None:
    provider = Provider(answer(THREE_TAGS))

    error = await analyze_failing(provider)

    assert error.retryable is True
    assert error.message == "AI returned an invalid result"
    assert provider.calls == 2


async def test_a_reply_cut_off_at_max_tokens_is_repaired() -> None:
    cut_off = answer('{"tags": ["dog", "park"', stop_reason="max_tokens")
    provider = Provider(cut_off, answer())

    analysis = await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert analysis.description == DESCRIPTION
    assert provider.calls == 2
    # The SDK rejects the unfinished JSON before we see it, so there's no reply to show.
    assert provider.body(1)["messages"][1] == {
        "role": "assistant",
        "content": "(no usable reply)",
    }


async def test_max_tokens_spent_on_thinking_is_repaired() -> None:
    provider = Provider(message(THINKING_ONLY, stop_reason="max_tokens"), answer())

    analysis = await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert analysis.description == DESCRIPTION
    assert provider.calls == 2
    assert provider.body(1)["messages"][1]["content"] == "(no usable reply)"


async def test_max_tokens_is_repaired_even_when_the_json_looks_complete() -> None:
    provider = Provider(answer(stop_reason="max_tokens"), answer())

    await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert provider.calls == 2
    assert provider.body(1)["messages"][1]["content"] == reply()


async def test_a_reply_that_stays_cut_off_is_a_retryable_error() -> None:
    provider = Provider(message(THINKING_ONLY, stop_reason="max_tokens"))

    error = await analyze_failing(provider)

    assert error.retryable is True
    assert error.message == "AI returned an invalid result"
    assert provider.calls == 2


async def test_a_failing_repair_request_is_classified_like_any_other() -> None:
    provider = Provider(answer(THREE_TAGS), failure(401))

    error = await analyze_failing(provider)

    assert error.retryable is False
    assert error.message == "AI service rejected the request"


# --- usage ---


async def test_usage_maps_tokens_and_cost_from_the_response() -> None:
    provider = Provider(answer(input_tokens=1500, output_tokens=120))

    analysis = await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert analysis.usage is not None
    assert analysis.usage.input_tokens == 1500
    assert analysis.usage.output_tokens == 120
    # 1,500 input tokens at $0.10 per million plus 120 output tokens at $0.50 per million.
    assert analysis.usage.cost_usd == pytest.approx(0.00021)
    assert analysis.usage.latency_ms >= 0


@pytest.mark.parametrize(
    ("model", "expected_cost"),
    [(HAIKU, 0.0004), (SONNET, 0.008), (OPUS, 0.016)],
)
async def test_usage_sums_tokens_and_cost_across_the_repair_attempt(
    model: str, expected_cost: float
) -> None:
    provider = Provider(
        answer(THREE_TAGS, model=model, input_tokens=1500, output_tokens=100),
        answer(model=model, input_tokens=1600, output_tokens=80),
    )

    analysis = await make_analyzer(provider, model).analyze(png_bytes(), "image/png")

    assert analysis.usage is not None
    assert analysis.usage.input_tokens == 3100
    assert analysis.usage.output_tokens == 180
    assert analysis.usage.cost_usd == pytest.approx(expected_cost)


async def test_an_unknown_model_has_tokens_but_no_cost() -> None:
    model = "claude-test-unknown"
    provider = Provider(answer(model=model, input_tokens=900, output_tokens=60))

    analysis = await make_analyzer(provider, model).analyze(png_bytes(), "image/png")

    assert analysis.usage is not None
    assert analysis.usage.input_tokens == 900
    assert analysis.usage.output_tokens == 60
    assert analysis.usage.cost_usd is None


async def test_cost_is_priced_on_the_model_that_answered_after_a_fallback() -> None:
    fallback_model = "claude-opus-4-8"
    served_by_fallback = [
        {
            "type": "fallback_message",
            "model": fallback_model,
            "input_tokens": 2000,
            "output_tokens": 200,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
    ]
    provider = Provider(
        answer(
            model=fallback_model,
            input_tokens=2000,
            output_tokens=200,
            iterations=served_by_fallback,
        )
    )

    analysis = await make_analyzer(provider, OPUS).analyze(png_bytes(), "image/png")

    assert analysis.usage is not None
    # Claude Opus 4.8 costs $5 and $25 per million tokens, where Opus 5.5 costs $4 and $20.
    assert analysis.usage.cost_usd == pytest.approx((2000 * 5 + 200 * 25) / 1_000_000)


async def test_latency_is_the_wall_time_of_the_whole_call_including_the_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter([100.0, 100.25])
    monkeypatch.setattr(
        anthropic_adapter, "time", SimpleNamespace(perf_counter=lambda: next(clock))
    )
    provider = Provider(answer(THREE_TAGS), answer())

    analysis = await make_analyzer(provider).analyze(png_bytes(), "image/png")

    assert analysis.usage is not None
    assert analysis.usage.latency_ms == 250


# --- price table ---


@pytest.mark.parametrize(
    ("model", "input_price", "output_price"),
    [(HAIKU, 0.10, 0.50), (SONNET, 2.00, 10.00), (OPUS, 4.00, 20.00)],
)
def test_the_benchmarked_models_have_their_list_prices(
    model: str, input_price: float, output_price: float
) -> None:
    price = PRICES[model]

    assert (price.input_usd_per_mtok, price.output_usd_per_mtok) == (input_price, output_price)


def test_every_price_names_its_source_and_date() -> None:
    for price in PRICES.values():
        assert price.source_url == PRICING_URL
        assert price.as_of == date(2026, 10, 20)


def test_cost_is_priced_per_million_tokens() -> None:
    assert cost_usd(SONNET, 1_000_000, 1_000_000) == pytest.approx(12.0)
    assert cost_usd(SONNET, 0, 0) == 0.0


def test_an_unknown_model_has_no_cost() -> None:
    assert cost_usd("claude-test-unknown", 1000, 100) is None


# --- factory ---


def test_factory_builds_the_analyzer_from_settings() -> None:
    analyzer = build_analyzer(
        Settings(
            ANALYZER="anthropic",
            ANTHROPIC_API_KEY="test-key",
            AI_MODEL=SONNET,
            AI_EFFORT="medium",
            AI_TIMEOUT_SECONDS=7.5,
        )
    )

    assert isinstance(analyzer, AnthropicAnalyzer)
    assert analyzer.analyzer_id == "anthropic:claude-sonnet-5-5:medium:v1"
    assert analyzer._client.timeout == 7.5
    assert analyzer._client.max_retries == 2


@pytest.mark.parametrize(
    ("missing", "overrides"),
    [("ANTHROPIC_API_KEY", {"AI_MODEL": HAIKU}), ("AI_MODEL", {"ANTHROPIC_API_KEY": "test-key"})],
)
def test_factory_names_the_missing_setting(missing: str, overrides: dict[str, str]) -> None:
    with pytest.raises(ValueError, match=missing):
        build_analyzer(Settings(ANALYZER="anthropic", **overrides))
