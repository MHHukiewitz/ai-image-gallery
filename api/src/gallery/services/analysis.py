"""The use case behind one analysis job: read the image, find its colors, tag it, store the result.

``AnalysisService.process`` never raises. Every failure becomes a ``queue.fail`` call, and every
job ends in exactly one structured log line, which doubles as the cost dashboard (see
docs/architecture.md §7). The log level of that line decides what reaches Sentry, whose logging
integration sends ``ERROR`` and above:

======================================  ===========
Outcome                                 Level
======================================  ===========
done                                    ``INFO``
corrupt image (the user's file)         ``INFO``
retryable ``AnalyzerError``             ``WARNING``
non-retryable ``AnalyzerError``         ``ERROR``
any other exception                     ``ERROR``, with the stack trace
======================================  ===========

The optional tag verifier is best-effort and never changes that outcome. When it fails, the
analyzer's tags are stored unchanged and the job still ends ``done``, after a ``WARNING`` for a
``TagVerificationError`` or an ``ERROR`` with the stack trace for anything else.
"""

import asyncio
import logging
import mimetypes
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from gallery.domain.colors import dominant_colors
from gallery.domain.errors import AnalyzerError, InvalidImageError, TagVerificationError
from gallery.domain.models import Analysis, AnalysisJob, AnalysisUsage, TagVerdict
from gallery.domain.tags import select_tags
from gallery.ports import AnalysisCache, ImageAnalyzer, JobQueue, ObjectStore, TagVerifier

logger = logging.getLogger(__name__)

type Outcome = Literal["done", "retry", "failed"]

# What users see in `last_error`. The details go to the logs, not to the gallery.
UNDECODABLE_IMAGE_ERROR = "Image could not be decoded"
UNEXPECTED_ERROR = "Unexpected error during analysis"


@dataclass(frozen=True, slots=True)
class _Verification:
    """What the tag verifier stage did for one job, for the job's log line.

    The counts are absent when the verifier failed, because the tags then stayed as they were.
    """

    latency_ms: int
    usage: AnalysisUsage | None = None
    tags_dropped: int | None = None
    tags_merged: int | None = None

    def log_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {"verifier_latency_ms": self.latency_ms}
        if self.usage is not None:
            fields.update(
                verifier_input_tokens=self.usage.input_tokens,
                verifier_cost_usd=self.usage.cost_usd,
            )
        if self.tags_dropped is not None and self.tags_merged is not None:
            fields.update(tags_dropped=self.tags_dropped, tags_merged=self.tags_merged)
        return fields


class AnalysisService:
    def __init__(
        self,
        queue: JobQueue,
        store: ObjectStore,
        analyzer: ImageAnalyzer,
        cache: AnalysisCache,
        *,
        max_attempts: int = 3,
        verifier: TagVerifier | None = None,
        min_tag_probability: float = 0.5,
    ) -> None:
        """`max_attempts` must match the queue's. It only tells a "retry" from a final "failed"
        in the log line, because the database makes that call.

        With a `verifier`, the analyzer's tags (cached or fresh) are checked against the image and
        tags it scores below `min_tag_probability` are dropped. The cache keeps the analyzer's
        tags, so the verifier can change or go away without invalidating it.
        """
        self._queue = queue
        self._store = store
        self._analyzer = analyzer
        self._cache = cache
        self._max_attempts = max_attempts
        self._verifier = verifier
        self._min_tag_probability = min_tag_probability

    async def process(self, job: AnalysisJob) -> None:
        """Analyze one claimed job and record the result. Never raises (cancellation aside)."""
        started = time.monotonic()
        colors: tuple[str, ...] = ()
        analysis: Analysis | None = None
        verification: _Verification | None = None
        cache_hit = False
        try:
            data = await self._store.get(job.original_path)
            # Decoding and quantizing are CPU work, so keep them off the event loop.
            colors = await asyncio.to_thread(dominant_colors, data)
            analysis, cache_hit = await self._analysis_for(job, data)
            tags, verification = await self._verified_tags(job, data, analysis)
            await self._queue.complete(
                job.image_id,
                tags=tags,
                description=analysis.description,
                colors=colors,
                analyzer_id=self._analyzer.analyzer_id,
            )
        except InvalidImageError:
            await self._fail(
                job,
                started,
                logging.INFO,
                error=UNDECODABLE_IMAGE_ERROR,
                colors=colors,
                retryable=False,
            )
        except AnalyzerError as error:
            await self._fail(
                job,
                started,
                logging.WARNING if error.retryable else logging.ERROR,
                error=error.message,
                colors=colors,
                retryable=error.retryable,
                cache_hit=cache_hit,
            )
        except Exception as unexpected:
            await self._fail(
                job,
                started,
                logging.ERROR,
                error=UNEXPECTED_ERROR,
                colors=colors,
                retryable=True,
                cache_hit=cache_hit,
                analysis=analysis,
                verification=verification,
                exception=unexpected,
            )
        else:
            self._log_job(
                logging.INFO,
                "analysis done",
                job,
                started,
                "done",
                cache_hit,
                analysis,
                verification=verification,
            )

    async def _analysis_for(self, job: AnalysisJob, data: bytes) -> tuple[Analysis, bool]:
        """Return the analysis and whether it came from the cache."""
        analyzer_id = self._analyzer.analyzer_id
        cached = await self._cache_get(job, analyzer_id)
        if cached is not None:
            return cached, True
        analysis = await self._analyzer.analyze(data, _mime_type(job))
        await self._cache_put(job, analyzer_id, analysis)
        return analysis, False

    async def _verified_tags(
        self, job: AnalysisJob, data: bytes, analysis: Analysis
    ) -> tuple[tuple[str, ...], _Verification | None]:
        """Return the tags to store, and what the verifier did (None when there is none).

        This never raises: the verifier is a refinement, so any failure in it keeps the analyzer's
        tags. A `TagVerificationError` is expected now and then, so it is a warning. Anything else
        is a bug, which is logged as an error so that it reaches Sentry.
        """
        if self._verifier is None:
            return analysis.tags, None
        started = time.monotonic()
        try:
            verdict = await self._verifier.verify(data, _mime_type(job), analysis.tags)
            tags = select_tags(analysis.tags, verdict, min_probability=self._min_tag_probability)
        except TagVerificationError as error:
            logger.warning(
                "tag verification failed, keeping the analyzer's tags",
                extra={"image_id": job.image_id, "error": str(error)},
            )
            return analysis.tags, _Verification(latency_ms=_elapsed_ms(started))
        except Exception:
            logger.exception(
                "tag verification crashed, keeping the analyzer's tags",
                extra={"image_id": job.image_id},
            )
            return analysis.tags, _Verification(latency_ms=_elapsed_ms(started))
        dropped, merged = _count_changes(analysis.tags, tags, verdict)
        return tags, _Verification(
            latency_ms=_elapsed_ms(started),
            usage=verdict.usage,
            tags_dropped=dropped,
            tags_merged=merged,
        )

    # The cache only saves money, so a cache failure must not fail the job: a failed `put` in
    # particular would throw away a paid analysis and pay for it again on the retry.
    async def _cache_get(self, job: AnalysisJob, analyzer_id: str) -> Analysis | None:
        try:
            return await self._cache.get(job.content_sha256, analyzer_id)
        except Exception:
            logger.warning(
                "analysis cache lookup failed, analyzing without it",
                exc_info=True,
                extra={"image_id": job.image_id},
            )
            return None

    async def _cache_put(self, job: AnalysisJob, analyzer_id: str, analysis: Analysis) -> None:
        try:
            await self._cache.put(job.content_sha256, analyzer_id, analysis)
        except Exception:
            logger.warning(
                "analysis cache write failed, the result is not cached",
                exc_info=True,
                extra={"image_id": job.image_id},
            )

    async def _fail(
        self,
        job: AnalysisJob,
        started: float,
        level: int,
        *,
        error: str,
        colors: Sequence[str],
        retryable: bool,
        cache_hit: bool = False,
        analysis: Analysis | None = None,
        verification: _Verification | None = None,
        exception: BaseException | None = None,
    ) -> None:
        """Hand the failure to the queue, then write the job's log line at `level`."""
        outcome: Outcome = "retry" if retryable and job.attempts < self._max_attempts else "failed"
        try:
            await self._queue.fail(job.image_id, error=error, colors=colors, retryable=retryable)
        except Exception:
            # Nothing more to try here. The job stays 'processing' and is reclaimed once stale.
            logger.exception(
                "could not record a failed analysis; the job will be reclaimed when it goes stale",
                extra={"image_id": job.image_id},
            )
        message = "analysis will be retried" if outcome == "retry" else "analysis failed"
        self._log_job(
            level,
            message,
            job,
            started,
            outcome,
            cache_hit,
            analysis,
            verification=verification,
            error=error,
            exception=exception,
        )

    def _log_job(
        self,
        level: int,
        message: str,
        job: AnalysisJob,
        started: float,
        outcome: Outcome,
        cache_hit: bool,
        analysis: Analysis | None,
        *,
        verification: _Verification | None = None,
        error: str | None = None,
        exception: BaseException | None = None,
    ) -> None:
        """Write the job's one summary line, with cost fields when the provider reported usage."""
        fields: dict[str, Any] = {
            "image_id": job.image_id,
            "outcome": outcome,
            "cache_hit": cache_hit,
            "duration_ms": _elapsed_ms(started),
            "attempt": job.attempts,
        }
        if error is not None:
            fields["error"] = error
        if analysis is not None and analysis.usage is not None:
            usage = analysis.usage
            fields.update(
                analyzer_id=self._analyzer.analyzer_id,
                provider_latency_ms=usage.latency_ms,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=usage.cost_usd,
            )
        if verification is not None:
            fields.update(verification.log_fields())
        logger.log(level, message, extra=fields, exc_info=exception)


def _mime_type(job: AnalysisJob) -> str:
    return mimetypes.guess_type(job.original_path)[0] or "application/octet-stream"


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _count_changes(
    original: Sequence[str], selected: Sequence[str], verdict: TagVerdict
) -> tuple[int, int]:
    """Return how many tags the verifier dropped and how many it merged into another tag."""
    kept = set(selected)
    removed = set(original) - kept
    merged = {dropped for kept_tag, dropped in verdict.merges if kept_tag in kept} & removed
    return len(removed) - len(merged), len(merged)
