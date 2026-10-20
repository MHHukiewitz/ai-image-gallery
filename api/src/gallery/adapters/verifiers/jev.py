"""TagVerifier that asks TypeSafe Jev, a typed decision model, whether each tag fits the image.

One request carries the image and every question: a yes/no `Noul` per tag, plus a `Choice` for
each pair of overlapping tags (see `duplicate_candidates`). Jev's image input is experimental
and opt-in, so this stage is off unless `TAG_VERIFIER=jev`.
"""

import asyncio
import io
import logging
import time
from collections.abc import Sequence

from PIL import Image as PilImage
from typesafe_sdk import (
    AsyncTypeSafeClient,
    Choice,
    Image,
    Noul,
    Question,
    RetryPolicy,
    SystemOneResponse,
    TypeSafeError,
)

from gallery.adapters.analyzers.images import VISION_JPEG_QUALITY, normalize_image
from gallery.domain.errors import AnalyzerError, TagVerificationError
from gallery.domain.models import AnalysisUsage, TagVerdict
from gallery.domain.tags import duplicate_candidates

logger = logging.getLogger(__name__)

# Jev rejects images with a side outside this range.
MIN_SIDE = 280
MAX_SIDE = 1568

# Image input is opt-in while TypeSafe calls it experimental.
IMAGE_OPT_IN_HEADERS = {"TypeSafe-Danger-Zone-Images": "true"}

# Jev 1.14's list price per million input tokens, checked on 2026-10-20 (docs/ai-service-choice.md,
# option H). Output tokens are free. Image token counts are unpublished, so this prices the
# `input_tokens` the API reports.
INPUT_USD_PER_MILLION_TOKENS = 0.042

# A failed verification only costs the filtering, while a retry would hold the job up. So the SDK
# does no retrying and `TAG_VERIFIER_TIMEOUT_SECONDS` bounds the whole call.
_NO_RETRY = RetryPolicy(max_retries=0)

# Neutral gray for the bars that make up the missing side of a very elongated image.
_PADDING_COLOR = (128, 128, 128)

_TAG_PREFIX = "tag"
_DEDUPE_PREFIX = "dedupe"
_NOUL_INSTRUCTIONS = (
    "Is the search tag '{tag}' an accurate description of something visible in this photo, "
    "or of its style?"
)
_CHOICE_INSTRUCTIONS = (
    "These two tags overlap. Which one is the better single search keyword for this photo?"
)


class JevTagVerifier:
    """Scores tags against the image and picks a winner between overlapping ones.

    The SDK's own retries are off and `timeout` bounds the whole call, so a slow or failing Jev
    costs a job at most `timeout` seconds. Every failure raises `TagVerificationError`.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout: float,
        client: AsyncTypeSafeClient | None = None,
    ) -> None:
        # The body is a JPEG, which zstd can't shrink, so compression would only cost CPU.
        self._client = client or AsyncTypeSafeClient(api_key=api_key, compression=False)
        self._model = model
        self._timeout = timeout

    async def verify(self, image: bytes, mime_type: str, tags: Sequence[str]) -> TagVerdict:
        """Score each of `tags` against `image`. `mime_type` is unused: the image is re-encoded."""
        unique_tags = tuple(dict.fromkeys(tags))
        if not unique_tags:
            return TagVerdict(probabilities={})
        prepared = await asyncio.to_thread(fit_image, image)
        pairs = duplicate_candidates(unique_tags)

        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._client.system_one(
                    state={"photo": Image(prepared)},
                    questions=_questions(unique_tags, pairs),
                    model=self._model,
                    retry=_NO_RETRY,
                    timeout=self._timeout,
                    extra_headers=IMAGE_OPT_IN_HEADERS,
                )
        except TimeoutError as error:
            raise TagVerificationError(
                f"Jev did not answer within {self._timeout} seconds"
            ) from error
        except TypeSafeError as error:
            # The SDK's messages carry the status and request id but never the API key.
            raise TagVerificationError(f"Jev request failed: {error}") from error
        latency_ms = round((time.perf_counter() - started) * 1000)
        return _verdict(response, unique_tags, pairs, latency_ms)


def fit_image(data: bytes) -> bytes:
    """Return JPEG bytes whose sides are both between `MIN_SIDE` and `MAX_SIDE` pixels.

    `normalize_image` first applies the EXIF orientation, puts transparency on white and caps the
    long side at 1024 px, which is already within Jev's range. Images with a short side under
    `MIN_SIDE` are then scaled up, and padded on the short side when scaling alone would push the
    long side over `MAX_SIDE`. Anything already in range is sent as normalized. Raises
    `TagVerificationError` when the bytes aren't an image.
    """
    try:
        _, jpeg = normalize_image(data)
    except AnalyzerError as error:
        raise TagVerificationError(error.message) from error
    with PilImage.open(io.BytesIO(jpeg)) as normalized:
        width, height = normalized.size
        if min(width, height) >= MIN_SIDE and max(width, height) <= MAX_SIDE:
            return jpeg
        scale = min(max(1.0, MIN_SIDE / min(width, height)), MAX_SIDE / max(width, height))
        resized = normalized.resize(
            (round(width * scale), round(height * scale)), PilImage.Resampling.LANCZOS
        )
    padded = _pad_to_minimum(resized)
    out = io.BytesIO()
    padded.save(out, format="JPEG", quality=VISION_JPEG_QUALITY)
    return out.getvalue()


def _pad_to_minimum(image: PilImage.Image) -> PilImage.Image:
    """Center `image` on a canvas that is at least `MIN_SIDE` px on both sides."""
    width, height = image.size
    canvas_size = (max(width, MIN_SIDE), max(height, MIN_SIDE))
    if canvas_size == image.size:
        return image
    canvas = PilImage.new("RGB", canvas_size, _PADDING_COLOR)
    canvas.paste(image, ((canvas_size[0] - width) // 2, (canvas_size[1] - height) // 2))
    return canvas


def _questions(tags: Sequence[str], pairs: Sequence[tuple[str, str]]) -> dict[str, Question]:
    """One yes/no question per tag, named `tag0`, `tag1`, ..., then one choice per pair."""
    questions: dict[str, Question] = {
        f"{_TAG_PREFIX}{index}": Noul(instructions=_NOUL_INSTRUCTIONS.format(tag=tag))
        for index, tag in enumerate(tags)
    }
    for index, (first, second) in enumerate(pairs):
        questions[f"{_DEDUPE_PREFIX}{index}"] = Choice(
            instructions=_CHOICE_INSTRUCTIONS, criteria={first: None, second: None}
        )
    return questions


def _verdict(
    response: SystemOneResponse,
    tags: Sequence[str],
    pairs: Sequence[tuple[str, str]],
    latency_ms: int,
) -> TagVerdict:
    """Map the answers back to tags. A question without a usable answer is left out."""
    probabilities: dict[str, float] = {}
    for index, tag in enumerate(tags):
        answer = response.nouls.get(f"{_TAG_PREFIX}{index}")
        if answer is not None:
            probabilities[tag] = answer.noul
    merges: list[tuple[str, str]] = []
    for index, (first, second) in enumerate(pairs):
        answer = response.choices.get(f"{_DEDUPE_PREFIX}{index}")
        if answer is not None and answer.choice in (first, second):
            kept = answer.choice
            merges.append((kept, second if kept == first else first))
    if len(probabilities) < len(tags):
        logger.warning(
            "Jev left some tags unscored, so they are kept",
            extra={"tags": len(tags), "scored": len(probabilities)},
        )
    input_tokens = response.usage.input_tokens
    cost_usd = (
        None if input_tokens is None else input_tokens * INPUT_USD_PER_MILLION_TOKENS / 1_000_000
    )
    usage = AnalysisUsage(
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=response.usage.output_tokens,
        cost_usd=cost_usd,
    )
    return TagVerdict(probabilities=probabilities, merges=tuple(merges), usage=usage)
