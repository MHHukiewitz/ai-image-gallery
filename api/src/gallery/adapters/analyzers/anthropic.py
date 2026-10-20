"""ImageAnalyzer that calls Claude through the official `anthropic` SDK.

This is the production analyzer. The request is one base64 image plus the shared prompt, and the
reply is constrained to `TagsAndDescription` with structured outputs (`messages.parse`).
"""

import base64
import dataclasses
import logging
import time
from asyncio import to_thread
from dataclasses import dataclass
from typing import Literal, cast

import anthropic
import pydantic
from anthropic import AsyncAnthropic
from anthropic.types import MessageParam
from anthropic.types.beta import BetaMessageParam
from anthropic.types.beta.parsed_beta_message import ParsedBetaMessage
from anthropic.types.parsed_message import ParsedMessage

from gallery.adapters.analyzers.images import normalize_image
from gallery.adapters.analyzers.pricing import cost_usd
from gallery.adapters.analyzers.prompt import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    USER_PROMPT,
    repair_prompt,
)
from gallery.domain.errors import AnalyzerError
from gallery.domain.models import Analysis, AnalysisUsage
from gallery.domain.tags import MIN_TAGS, normalize_description, normalize_tags

logger = logging.getLogger(__name__)

Effort = Literal["low", "medium", "high", "xhigh", "max"]

# The SDK retries rate limits, 5xx, 408/409 and connection errors with backoff. Nothing here
# retries on top of that. The value is its default, pinned so a change of default is a diff.
SDK_MAX_RETRIES = 2

# Non-streaming ceiling that keeps a request clear of HTTP timeouts. Thinking counts toward it, so
# it stays well above the few hundred tokens a reply needs.
MAX_TOKENS = 16_000

# These models accept server-side refusal fallbacks. Haiku 5.5 has none, and sending the
# parameter there is an error.
_FALLBACK_MODELS = frozenset({"claude-opus-5-5", "claude-sonnet-5-5"})
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# These messages reach users through the job's error field, so they say nothing about providers.
_DECLINED = "The AI declined to analyze this image"
_UNAVAILABLE = "AI service unavailable"
_REJECTED = "AI service rejected the request"
_INVALID_RESULT = "AI returned an invalid result"

_LOGGED_MESSAGE_CHARS = 200
_NO_REPLY_TEXT = "(no usable reply)"


class TagsAndDescription(pydantic.BaseModel):
    """The reply schema. The 5-10 tag range is in the prompt and enforced after normalization."""

    tags: list[str]
    description: str


class AnthropicAnalyzer:
    """Asks Claude for tags and a description, and repairs an unusable reply once.

    `thinking` is never sent: the 5.5 models run adaptive thinking when it's omitted, and some
    reject `disabled`. `effort` is the knob for how much they think, and it is part of
    `analyzer_id` because it changes the output. A reply that is cut off or has too few tags is
    corrected with one follow-up request. A refusal is permanent. The SDK does the retrying of
    transient failures, so `AI_TIMEOUT_SECONDS` bounds each attempt and a call can take up to
    `SDK_MAX_RETRIES + 1` times that.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        effort: Effort,
        timeout: float,
        client: AsyncAnthropic | None = None,
    ) -> None:
        sdk_client = client or AsyncAnthropic(api_key=api_key, max_retries=SDK_MAX_RETRIES)
        self._client = sdk_client.with_options(timeout=timeout)
        self._model = model
        self._effort = effort

    @property
    def analyzer_id(self) -> str:
        return f"anthropic:{self._model}:{self._effort}:{PROMPT_VERSION}"

    async def analyze(self, image: bytes, mime_type: str) -> Analysis:
        """Tag and describe `image`. `mime_type` is unused: the image is re-encoded as JPEG."""
        started = time.perf_counter()
        prepared_mime_type, prepared = await to_thread(normalize_image, image)
        messages = _initial_messages(prepared_mime_type, prepared)
        usage = _UsageTotals()

        try:
            analysis = await self._attempt(messages, usage)
        except _UnusableReply as problem:
            logger.warning(
                "analyzer reply unusable, asking for a correction",
                extra={"model": self._model, "problem": problem.problem},
            )
            correction: list[MessageParam] = [
                *messages,
                {"role": "assistant", "content": problem.reply_text or _NO_REPLY_TEXT},
                {"role": "user", "content": repair_prompt(problem.problem)},
            ]
            try:
                analysis = await self._attempt(correction, usage)
            except _UnusableReply as second_problem:
                logger.warning(
                    "analyzer reply still unusable after correction",
                    extra={"model": self._model, "problem": second_problem.problem},
                )
                raise AnalyzerError(_INVALID_RESULT, retryable=True) from second_problem

        latency_ms = round((time.perf_counter() - started) * 1000)
        return dataclasses.replace(analysis, usage=usage.to_usage(latency_ms))

    async def _attempt(self, messages: list[MessageParam], usage: "_UsageTotals") -> Analysis:
        """Make one request and turn its reply into an `Analysis`.

        Raises `_UnusableReply` when a correction may help, and `AnalyzerError` when it can't.
        """
        reply = await self._request(messages)
        usage.add(reply)

        if reply.stop_reason == "refusal":
            # With fallbacks on, a refusal here means every model in the chain declined.
            logger.info(
                "analyzer declined to analyze an image",
                extra={
                    "model": reply.model,
                    "category": reply.refusal_category,
                    "request_id": reply.request_id,
                },
            )
            raise AnalyzerError(_DECLINED, retryable=False)
        if reply.stop_reason == "max_tokens":
            raise _UnusableReply("the reply was cut off before it was complete", reply.text)
        if reply.parsed is None:
            raise _UnusableReply("the reply contained no JSON object", reply.text)

        tags = normalize_tags(reply.parsed.tags)
        if len(tags) < MIN_TAGS:
            raise _UnusableReply(
                f"only {len(tags)} usable tags remained after cleanup; at least {MIN_TAGS} "
                "distinct tags of one or two words are required",
                reply.text,
            )
        description = normalize_description(reply.parsed.description)
        if not description:
            raise _UnusableReply('"description" was empty', reply.text)
        return Analysis(tags=tags, description=description)

    async def _request(self, messages: list[MessageParam]) -> "_Reply":
        """Send one request. SDK errors become `AnalyzerError`, most specific class first."""
        try:
            return await self._parse(messages)
        except pydantic.ValidationError as error:
            # `parse` validates the text before returning the message, so a cut-off reply raises
            # here and its token usage is lost. That only under-reports the rare repair case.
            raise _UnusableReply(
                "the reply was cut off or did not match the schema", reply_text=""
            ) from error
        except (
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.BadRequestError,
            anthropic.NotFoundError,
        ) as error:
            raise self._rejected(error) from error
        except anthropic.RateLimitError as error:
            raise self._unavailable(error) from error
        except anthropic.APIStatusError as error:
            # Reaching this means the SDK's own retries are spent when it's a 5xx. 408 and 409 are
            # transient too. Anything else (402 billing, 413, 422) won't change by trying again.
            if error.status_code >= 500 or error.status_code in (408, 409):
                raise self._unavailable(error) from error
            raise self._rejected(error) from error
        except anthropic.APIConnectionError as error:  # includes APITimeoutError
            raise self._unavailable(error) from error

    async def _parse(self, messages: list[MessageParam]) -> "_Reply":
        if self._model in _FALLBACK_MODELS:
            # Server-side fallbacks live on the beta endpoint. "default" lets Anthropic pick the
            # substitute for the refusal category instead of us naming a model.
            beta_message = await self._client.beta.messages.parse(
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                # The block shapes we send are identical in both namespaces.
                messages=cast("list[BetaMessageParam]", messages),
                output_format=TagsAndDescription,
                output_config={"effort": self._effort},
                betas=[_FALLBACK_BETA],
                fallbacks="default",
            )
            return _Reply.from_message(beta_message)
        message = await self._client.messages.parse(
            model=self._model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=messages,
            output_format=TagsAndDescription,
            output_config={"effort": self._effort},
        )
        return _Reply.from_message(message)

    def _unavailable(self, error: anthropic.APIError) -> AnalyzerError:
        # A later job attempt may succeed, so this is a warning rather than an error for Sentry.
        logger.warning("analyzer provider unavailable", extra=self._error_fields(error))
        return AnalyzerError(_UNAVAILABLE, retryable=True)

    def _rejected(self, error: anthropic.APIError) -> AnalyzerError:
        # A bad key, a wrong model id or a malformed request is ours to fix, so it reaches Sentry.
        logger.error("analyzer provider rejected the request", extra=self._error_fields(error))
        return AnalyzerError(_REJECTED, retryable=False)

    def _error_fields(self, error: anthropic.APIError) -> dict[str, object]:
        status_code = error.status_code if isinstance(error, anthropic.APIStatusError) else None
        request_id = error.request_id if isinstance(error, anthropic.APIStatusError) else None
        return {
            "model": self._model,
            "error_type": type(error).__name__,
            "status_code": status_code,
            "request_id": request_id,
            "provider_message": error.message[:_LOGGED_MESSAGE_CHARS],
        }


class _UnusableReply(Exception):
    """The model answered, but not with a result we can store. A correction may fix it."""

    def __init__(self, problem: str, reply_text: str) -> None:
        super().__init__(problem)
        self.problem = problem
        self.reply_text = reply_text


@dataclass(frozen=True, slots=True)
class _Reply:
    """The parts of a parsed Messages API response that the analyzer reads."""

    stop_reason: str | None
    refusal_category: str | None
    parsed: TagsAndDescription | None
    text: str
    input_tokens: int
    output_tokens: int
    # The model that produced the message. It differs from the requested one after a fallback.
    model: str
    request_id: str | None

    @classmethod
    def from_message(
        cls,
        message: ParsedMessage[TagsAndDescription] | ParsedBetaMessage[TagsAndDescription],
    ) -> "_Reply":
        return cls(
            stop_reason=message.stop_reason,
            refusal_category=message.stop_details.category if message.stop_details else None,
            parsed=message.parsed_output,
            text="".join(block.text for block in message.content if block.type == "text"),
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            model=message.model,
            request_id=message._request_id,
        )


@dataclass
class _UsageTotals:
    """Tokens and cost summed over every request behind one analysis."""

    input_tokens: int = 0
    output_tokens: int = 0
    # None as soon as one request ran on a model without a known price.
    cost_usd: float | None = 0.0

    def add(self, reply: _Reply) -> None:
        self.input_tokens += reply.input_tokens
        self.output_tokens += reply.output_tokens
        request_cost = cost_usd(reply.model, reply.input_tokens, reply.output_tokens)
        if request_cost is None or self.cost_usd is None:
            self.cost_usd = None
        else:
            self.cost_usd += request_cost

    def to_usage(self, latency_ms: int) -> AnalysisUsage:
        return AnalysisUsage(
            latency_ms=latency_ms,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=self.cost_usd,
        )


def _initial_messages(mime_type: str, image: bytes) -> list[MessageParam]:
    """One user turn: the image first, then the instruction, as the vision docs recommend."""
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": cast("Literal['image/jpeg']", mime_type),
                        "data": base64.standard_b64encode(image).decode("ascii"),
                    },
                },
                {"type": "text", "text": USER_PROMPT},
            ],
        }
    ]
