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
"""

import asyncio
import logging
import mimetypes
import time
from collections.abc import Sequence
from typing import Any, Literal

from gallery.domain.colors import dominant_colors
from gallery.domain.errors import AnalyzerError, InvalidImageError
from gallery.domain.models import Analysis, AnalysisJob
from gallery.ports import AnalysisCache, ImageAnalyzer, JobQueue, ObjectStore

logger = logging.getLogger(__name__)

type Outcome = Literal["done", "retry", "failed"]

# What users see in `last_error`. The details go to the logs, not to the gallery.
UNDECODABLE_IMAGE_ERROR = "Image could not be decoded"
UNEXPECTED_ERROR = "Unexpected error during analysis"


class AnalysisService:
    def __init__(
        self,
        queue: JobQueue,
        store: ObjectStore,
        analyzer: ImageAnalyzer,
        cache: AnalysisCache,
        *,
        max_attempts: int = 3,
    ) -> None:
        """`max_attempts` must match the queue's. It only tells a "retry" from a final "failed"
        in the log line, because the database makes that call.
        """
        self._queue = queue
        self._store = store
        self._analyzer = analyzer
        self._cache = cache
        self._max_attempts = max_attempts

    async def process(self, job: AnalysisJob) -> None:
        """Analyze one claimed job and record the result. Never raises (cancellation aside)."""
        started = time.monotonic()
        colors: tuple[str, ...] = ()
        analysis: Analysis | None = None
        cache_hit = False
        try:
            data = await self._store.get(job.original_path)
            # Decoding and quantizing are CPU work, so keep them off the event loop.
            colors = await asyncio.to_thread(dominant_colors, data)
            analysis, cache_hit = await self._analysis_for(job, data)
            await self._queue.complete(
                job.image_id,
                tags=analysis.tags,
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
                exception=unexpected,
            )
        else:
            self._log_job(logging.INFO, "analysis done", job, started, "done", cache_hit, analysis)

    async def _analysis_for(self, job: AnalysisJob, data: bytes) -> tuple[Analysis, bool]:
        """Return the analysis and whether it came from the cache."""
        analyzer_id = self._analyzer.analyzer_id
        cached = await self._cache_get(job, analyzer_id)
        if cached is not None:
            return cached, True
        mime_type = mimetypes.guess_type(job.original_path)[0] or "application/octet-stream"
        analysis = await self._analyzer.analyze(data, mime_type)
        await self._cache_put(job, analyzer_id, analysis)
        return analysis, False

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
        error: str | None = None,
        exception: BaseException | None = None,
    ) -> None:
        """Write the job's one summary line, with cost fields when the provider reported usage."""
        fields: dict[str, Any] = {
            "image_id": job.image_id,
            "outcome": outcome,
            "cache_hit": cache_hit,
            "duration_ms": round((time.monotonic() - started) * 1000),
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
        logger.log(level, message, extra=fields, exc_info=exception)
