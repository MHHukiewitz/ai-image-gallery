"""The use case behind one ``analyze_image`` job: read the image, find its colors, tag it, store
the result.

``AnalysisService.process`` is the worker's handler for that kind of job, and it never raises.
Every failure becomes a ``queue.fail_analysis`` call (``queue.fail`` when the image couldn't even
be looked up), and every job ends in exactly one structured log line, which doubles as the cost
dashboard (see docs/architecture.md §7). The log level of that line decides what reaches Sentry,
whose logging integration sends ``ERROR`` and above:

======================================  ===========
Outcome                                 Level
======================================  ===========
done                                    ``INFO``
skipped (the analysis already ended)    ``INFO``
corrupt image (the user's file)         ``INFO``
retryable ``AnalyzerError``             ``WARNING``
non-retryable ``AnalyzerError``         ``ERROR``
any other exception                     ``ERROR``, with the stack trace
======================================  ===========

A job is skipped when its image is gone or its analysis already ended outside the job: the worker
from before the jobs table finishes the uploads that arrive between the migration and the deploy,
while their jobs wait. The job is then closed with the same outcome, and the analyzer isn't called.

The optional tag verifier is best-effort and never changes that outcome. When it fails, the
analyzer's tags are stored unchanged and the job still ends ``done``, after a ``WARNING`` for a
``TagVerificationError`` or an ``ERROR`` with the stack trace for anything else.

So is the optional embedding of the final tags and description, which "find similar" scores on. The
image is completed first and embedded after. When that fails, the job still ends ``done`` after a
``WARNING``, and the image matches on its tags until ``gallery.backfill_embeddings`` fills it in.
"""

import asyncio
import hashlib
import logging
import mimetypes
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from gallery.domain.colors import dominant_colors
from gallery.domain.embedding_text import embedding_text
from gallery.domain.errors import AnalyzerError, InvalidImageError, TagVerificationError
from gallery.domain.models import (
    Analysis,
    AnalysisStatus,
    AnalysisTarget,
    AnalysisUsage,
    Job,
    TagVerdict,
)
from gallery.domain.tags import select_tags
from gallery.domain.validation import inspect_image
from gallery.ports import (
    AnalysisCache,
    ImageAnalyzer,
    JobQueue,
    ObjectStore,
    TagVerifier,
    TextEmbedder,
)

logger = logging.getLogger(__name__)

# The kind of job this service runs.
ANALYZE_IMAGE = "analyze_image"

type Outcome = Literal["done", "skipped", "retry", "failed"]

# What users see in `last_error`. The details go to the logs, not to the gallery.
UNDECODABLE_IMAGE_ERROR = "Image could not be decoded"
UNEXPECTED_ERROR = "Unexpected error during analysis"

_FINISHED = (AnalysisStatus.DONE, AnalysisStatus.FAILED)


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


@dataclass(frozen=True, slots=True)
class _Embedding:
    """What the embedding stage did for one job, for the job's log line."""

    latency_ms: int
    stored: bool

    def log_fields(self) -> dict[str, Any]:
        return {"embedding_ms": self.latency_ms, "embedding_stored": self.stored}


class AnalysisService:
    def __init__(
        self,
        queue: JobQueue,
        store: ObjectStore,
        analyzer: ImageAnalyzer,
        cache: AnalysisCache,
        *,
        max_bytes: int,
        max_pixels: int,
        max_attempts: int = 3,
        verifier: TagVerifier | None = None,
        min_tag_probability: float = 0.5,
        embedder: TextEmbedder | None = None,
    ) -> None:
        """`max_bytes` and `max_pixels` are the upload limits. The API enforces them on its own
        uploads, but a row inserted straight through PostgREST skips that, so the worker checks the
        stored bytes against them before it decodes a pixel.

        `max_attempts` must match the queue's. It only tells a "retry" from a final "failed"
        in the log line, because the database makes that call.

        With a `verifier`, the analyzer's tags (cached or fresh) are checked against the image and
        tags it scores below `min_tag_probability` are dropped. The cache keeps the analyzer's
        tags, so the verifier can change or go away without invalidating it.

        With an `embedder`, the final tags and description are embedded after the job completes
        and the vector is stored on the image. The embedding is never cached, because it follows
        the final tags.
        """
        self._queue = queue
        self._store = store
        self._analyzer = analyzer
        self._cache = cache
        self._max_bytes = max_bytes
        self._max_pixels = max_pixels
        self._max_attempts = max_attempts
        self._verifier = verifier
        self._min_tag_probability = min_tag_probability
        self._embedder = embedder

    async def process(self, job: Job) -> None:
        """Analyze the image of one claimed analyze_image job and record the result.

        A job whose analysis already ended is closed without analyzing. Never raises
        (cancellation aside).
        """
        started = time.monotonic()
        try:
            target = await self._queue.analysis_target(job.id)
        except Exception as unexpected:
            # The image's state is unknown, so fail only the job and leave the image's colors.
            await self._fail(
                job,
                started,
                logging.ERROR,
                error=UNEXPECTED_ERROR,
                colors=None,
                retryable=True,
                exception=unexpected,
            )
            return
        if target is None or target.status in _FINISHED:
            await self._close_finished(job, target, started)
            return
        await self._analyze(job, target, started)

    async def _analyze(self, job: Job, target: AnalysisTarget, started: float) -> None:
        colors: tuple[str, ...] = ()
        analysis: Analysis | None = None
        verification: _Verification | None = None
        embedding: _Embedding | None = None
        cache_hit = False
        try:
            data = await self._store.get(target.original_path)
            # Decoding and quantizing are CPU work, so keep them off the event loop.
            colors = await asyncio.to_thread(self._inspect_then_colors, data)
            # The cache key is the digest of the bytes just downloaded, never the row's
            # content_sha256: that is the hash of the file as uploaded, which the API strips of
            # metadata before storing (#85), so it often differs, and a user can also write it
            # through PostgREST. A mismatch is expected, so it is not logged.
            content_sha256 = await asyncio.to_thread(_sha256_hex, data)
            analysis, cache_hit = await self._analysis_for(target, data, content_sha256)
            tags, verification = await self._verified_tags(target, data, analysis)
            await self._queue.complete_analysis(
                job.id,
                tags=tags,
                description=analysis.description,
                colors=colors,
                analyzer_id=self._analyzer.analyzer_id,
            )
            embedding = await self._store_embedding(target, tags, analysis.description)
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
                embedding=embedding,
            )

    def _inspect_then_colors(self, data: bytes) -> tuple[str, ...]:
        """Refuse bytes over the upload limits, then find the colors.

        The check reads only the header, so it runs before anything allocates a pixel buffer.
        Raises InvalidImageError.
        """
        inspect_image(data, max_bytes=self._max_bytes, max_pixels=self._max_pixels)
        return dominant_colors(data)

    async def _close_finished(
        self, job: Job, target: AnalysisTarget | None, started: float
    ) -> None:
        """Close a job with nothing left to do, without reading the image or calling the analyzer.

        The image is gone, or its analysis already ended outside this job. The job ends the way
        the analysis did, and the image keeps the result it has.
        """
        try:
            if target is not None and target.status is AnalysisStatus.FAILED:
                await self._queue.fail(
                    job.id, error=target.error or UNEXPECTED_ERROR, retryable=False
                )
            else:
                await self._queue.complete(job.id)
        except Exception:
            logger.warning(
                "could not close a job whose analysis already ended; it will be reclaimed when "
                "it goes stale",
                exc_info=True,
                extra={"job_id": job.id, "image_id": job.image_id},
            )
        if target is None:
            message, status = "image is gone, job closed", None
        else:
            message, status = "analysis already ended, job closed", target.status
        self._log_job(logging.INFO, message, job, started, "skipped", False, None, status=status)

    async def _analysis_for(
        self, target: AnalysisTarget, data: bytes, content_sha256: str
    ) -> tuple[Analysis, bool]:
        """Return the analysis and whether it came from the cache.

        `content_sha256` is the digest of `data`, the bytes this worker downloaded. The cache is
        shared by all tenants, so it must never be keyed by the row's own `content_sha256`: that
        is the hash of the uploaded file, not of the stripped original (#85), and a user can write
        it through PostgREST.
        """
        analyzer_id = self._analyzer.analyzer_id
        cached = await self._cache_get(target, content_sha256, analyzer_id)
        if cached is not None:
            return cached, True
        analysis = await self._analyzer.analyze(data, _mime_type(target))
        await self._cache_put(target, content_sha256, analyzer_id, analysis)
        return analysis, False

    async def _verified_tags(
        self, target: AnalysisTarget, data: bytes, analysis: Analysis
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
            verdict = await self._verifier.verify(data, _mime_type(target), analysis.tags)
            tags = select_tags(analysis.tags, verdict, min_probability=self._min_tag_probability)
        except TagVerificationError as error:
            logger.warning(
                "tag verification failed, keeping the analyzer's tags",
                extra={"image_id": target.image_id, "error": str(error)},
            )
            return analysis.tags, _Verification(latency_ms=_elapsed_ms(started))
        except Exception:
            logger.exception(
                "tag verification crashed, keeping the analyzer's tags",
                extra={"image_id": target.image_id},
            )
            return analysis.tags, _Verification(latency_ms=_elapsed_ms(started))
        dropped, merged = _count_changes(analysis.tags, tags, verdict)
        return tags, _Verification(
            latency_ms=_elapsed_ms(started),
            usage=verdict.usage,
            tags_dropped=dropped,
            tags_merged=merged,
        )

    async def _store_embedding(
        self, target: AnalysisTarget, tags: Sequence[str], description: str
    ) -> _Embedding | None:
        """Embed the final text and store the vector. Return what happened (None when skipped).

        This runs after `complete_analysis` on purpose: completing an analysis drops an embedding
        that no longer matches the new text, so a vector stored earlier would be thrown away. It
        never raises. An embedding only refines "find similar", which falls back to the tags, so a
        failure here is a warning and the job stays done.
        """
        if self._embedder is None:
            return None
        text = embedding_text(tags, description)
        if not text:
            return None
        started = time.monotonic()
        try:
            [vector] = await self._embedder.embed([text])
            await self._queue.store_embedding(target.image_id, vector, self._embedder.model_id)
        except Exception:
            logger.warning(
                "embedding failed, the image keeps matching on its tags",
                exc_info=True,
                extra={"image_id": target.image_id},
            )
            return _Embedding(latency_ms=_elapsed_ms(started), stored=False)
        return _Embedding(latency_ms=_elapsed_ms(started), stored=True)

    # The cache only saves money, so a cache failure must not fail the job: a failed `put` in
    # particular would throw away a paid analysis and pay for it again on the retry.
    async def _cache_get(
        self, target: AnalysisTarget, content_sha256: str, analyzer_id: str
    ) -> Analysis | None:
        try:
            return await self._cache.get(content_sha256, analyzer_id)
        except Exception:
            logger.warning(
                "analysis cache lookup failed, analyzing without it",
                exc_info=True,
                extra={"image_id": target.image_id},
            )
            return None

    async def _cache_put(
        self, target: AnalysisTarget, content_sha256: str, analyzer_id: str, analysis: Analysis
    ) -> None:
        try:
            await self._cache.put(content_sha256, analyzer_id, analysis)
        except Exception:
            logger.warning(
                "analysis cache write failed, the result is not cached",
                exc_info=True,
                extra={"image_id": target.image_id},
            )

    async def _fail(
        self,
        job: Job,
        started: float,
        level: int,
        *,
        error: str,
        colors: Sequence[str] | None,
        retryable: bool,
        cache_hit: bool = False,
        analysis: Analysis | None = None,
        verification: _Verification | None = None,
        exception: BaseException | None = None,
    ) -> None:
        """Hand the failure to the queue, then write the job's log line at `level`.

        `colors` are stored on the image. None fails the job alone, for a failure that came before
        the image was even looked up.
        """
        outcome: Outcome = "retry" if retryable and job.attempts < self._max_attempts else "failed"
        try:
            if colors is None:
                await self._queue.fail(job.id, error=error, retryable=retryable)
            else:
                await self._queue.fail_analysis(
                    job.id, error=error, colors=colors, retryable=retryable
                )
        except Exception:
            # Nothing more to try here. The job stays 'processing' and is reclaimed once stale.
            logger.exception(
                "could not record a failed analysis; the job will be reclaimed when it goes stale",
                extra={"job_id": job.id, "image_id": job.image_id},
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
        job: Job,
        started: float,
        outcome: Outcome,
        cache_hit: bool,
        analysis: Analysis | None,
        *,
        verification: _Verification | None = None,
        embedding: _Embedding | None = None,
        error: str | None = None,
        status: AnalysisStatus | None = None,
        exception: BaseException | None = None,
    ) -> None:
        """Write the job's one summary line, with cost fields when the provider reported usage.

        `status` is the state a skipped job found its image's analysis in.
        """
        fields: dict[str, Any] = {
            "job_id": job.id,
            "image_id": job.image_id,
            "outcome": outcome,
            "cache_hit": cache_hit,
            "duration_ms": _elapsed_ms(started),
            "attempt": job.attempts,
        }
        if error is not None:
            fields["error"] = error
        if status is not None:
            fields["analysis_status"] = str(status)
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
        if embedding is not None:
            fields.update(embedding.log_fields())
        logger.log(level, message, extra=fields, exc_info=exception)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mime_type(target: AnalysisTarget) -> str:
    return mimetypes.guess_type(target.original_path)[0] or "application/octet-stream"


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
