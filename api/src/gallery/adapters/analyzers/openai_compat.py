"""ImageAnalyzer for any OpenAI-compatible chat completions API.

That covers OpenRouter, OpenAI, Gemini's compatibility endpoint, and self-hosted servers such as
Ollama. The benchmark (#6) uses it for the non-Claude reference model.
"""

import dataclasses
import logging
import time
from asyncio import sleep, to_thread
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import openai
from openai import AsyncOpenAI
from openai.types import CompletionUsage
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam

from gallery.adapters.analyzers.images import image_data_url, normalize_image
from gallery.adapters.analyzers.parsing import InvalidAnalysisError, parse_analysis
from gallery.adapters.analyzers.prompt import (
    PROMPT_VERSION,
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    USER_PROMPT,
    repair_prompt,
)
from gallery.domain.errors import AnalyzerError
from gallery.domain.models import Analysis, AnalysisUsage

logger = logging.getLogger(__name__)

# Seconds to wait before the 2nd and 3rd attempt of a request that failed transiently.
RETRY_DELAYS_SECONDS = (0.5, 2.0)

# These messages reach users through the job's error field, so they say nothing about providers.
_UNAVAILABLE = "AI service unavailable"
_REJECTED = "AI service rejected the request"
_INVALID_RESULT = "AI returned an invalid result"

_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {"name": "image_analysis", "strict": True, "schema": RESPONSE_SCHEMA},
}
_LOGGED_MESSAGE_CHARS = 200


class OpenAICompatAnalyzer:
    """Asks a vision model for tags and a description, and repairs a bad reply once.

    A request that fails for a transient reason is retried with `RETRY_DELAYS_SECONDS`. If the
    reply isn't a valid analysis, the model is asked once to correct it. Failures that a later
    job attempt may fix raise a retryable `AnalyzerError`. Rejected requests (bad key, bad
    request) raise a non-retryable one.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float,
        client: AsyncOpenAI | None = None,
    ) -> None:
        # Retries happen in `_complete`, so the SDK's own are switched off even on a client the
        # caller passed in. Otherwise a failing request would be retried 3 times over.
        sdk_client = client or AsyncOpenAI(base_url=base_url, api_key=api_key)
        self._client = sdk_client.with_options(max_retries=0, timeout=timeout)
        self._model = model
        self._extra_body = _cost_reporting_body(str(self._client.base_url))

    @property
    def analyzer_id(self) -> str:
        return f"openai_compat:{self._model}:{PROMPT_VERSION}"

    async def analyze(self, image: bytes, mime_type: str) -> Analysis:
        """Tag and describe `image`. `mime_type` is unused: the image is re-encoded as JPEG."""
        started = time.perf_counter()
        prepared_mime_type, prepared = await to_thread(normalize_image, image)
        messages = _initial_messages(image_data_url(prepared_mime_type, prepared))
        usage = _UsageTotals()

        reply = await self._complete(messages, usage)
        try:
            analysis = parse_analysis(reply)
        except InvalidAnalysisError as problem:
            logger.warning(
                "analyzer reply unusable, asking for a correction",
                extra={"model": self._model, "problem": str(problem), "reply_chars": len(reply)},
            )
            correction: list[ChatCompletionMessageParam] = [
                *messages,
                {"role": "assistant", "content": reply or "(empty reply)"},
                {"role": "user", "content": repair_prompt(str(problem))},
            ]
            reply = await self._complete(correction, usage)
            try:
                analysis = parse_analysis(reply)
            except InvalidAnalysisError as second_problem:
                logger.warning(
                    "analyzer reply still unusable after correction",
                    extra={
                        "model": self._model,
                        "problem": str(second_problem),
                        "reply_chars": len(reply),
                    },
                )
                raise AnalyzerError(_INVALID_RESULT, retryable=True) from second_problem

        latency_ms = round((time.perf_counter() - started) * 1000)
        return dataclasses.replace(analysis, usage=usage.to_usage(latency_ms))

    async def _complete(
        self, messages: list[ChatCompletionMessageParam], usage: "_UsageTotals"
    ) -> str:
        """Run one chat completion, retrying transient failures, and return the reply text."""
        failure: openai.APIError | None = None
        for attempt, delay in enumerate((*RETRY_DELAYS_SECONDS, None), start=1):
            try:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    response_format=_RESPONSE_FORMAT,
                    extra_body=self._extra_body,
                )
            except openai.APIConnectionError as error:  # includes timeouts
                failure = error
            except openai.APIStatusError as error:
                if not _is_transient_status(error.status_code):
                    logger.error(
                        "analyzer provider rejected the request",
                        extra={
                            "model": self._model,
                            "status_code": error.status_code,
                            "provider_message": error.message[:_LOGGED_MESSAGE_CHARS],
                        },
                    )
                    raise AnalyzerError(_REJECTED, retryable=False) from error
                failure = error
            else:
                usage.add(response.usage)
                return _reply_text(response)

            logger.warning(
                "analyzer request failed",
                extra={
                    "model": self._model,
                    "attempt": attempt,
                    "will_retry": delay is not None,
                    "status_code": getattr(failure, "status_code", None),
                    "error_type": type(failure).__name__,
                },
            )
            if delay is not None:
                await sleep(delay)
        raise AnalyzerError(_UNAVAILABLE, retryable=True) from failure


@dataclass
class _UsageTotals:
    """Tokens and cost summed over every provider call behind one analysis."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None

    def add(self, usage: CompletionUsage | None) -> None:
        """Add one response's `usage`. A provider that reports nothing leaves the totals None."""
        if usage is None:
            return
        # getattr: compatible servers differ in which fields they fill in.
        self.input_tokens = _sum_known(self.input_tokens, getattr(usage, "prompt_tokens", None))
        self.output_tokens = _sum_known(
            self.output_tokens, getattr(usage, "completion_tokens", None)
        )
        self.cost_usd = _sum_known(self.cost_usd, _reported_cost(usage))

    def to_usage(self, latency_ms: int) -> AnalysisUsage:
        return AnalysisUsage(
            latency_ms=latency_ms,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=self.cost_usd,
        )


def _sum_known[T: (int, float)](total: T | None, amount: T | None) -> T | None:
    if amount is None:
        return total
    return amount if total is None else total + amount


def _reported_cost(usage: CompletionUsage) -> float | None:
    # OpenRouter adds `cost` (USD) to `usage`. Plain OpenAI doesn't, so it stays unknown.
    cost = getattr(usage, "cost", None)
    if isinstance(cost, bool) or not isinstance(cost, int | float):
        return None
    return float(cost)


def _cost_reporting_body(base_url: str) -> dict[str, Any] | None:
    """Ask OpenRouter to report cost. Other providers reject unknown request fields."""
    host = urlparse(base_url).hostname or ""
    if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
        return {"usage": {"include": True}}
    return None


def _is_transient_status(status_code: int) -> bool:
    return status_code in (408, 429) or status_code >= 500


def _reply_text(response: ChatCompletion) -> str:
    # OpenRouter sometimes answers 200 with an error object and no choices.
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    return choices[0].message.content or ""


def _initial_messages(data_url: str) -> list[ChatCompletionMessageParam]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": USER_PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]
