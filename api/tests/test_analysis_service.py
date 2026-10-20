import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

import pytest
from PIL import Image

from gallery.domain.colors import dominant_colors
from gallery.domain.errors import AnalyzerError
from gallery.domain.models import Analysis, AnalysisJob, AnalysisUsage
from gallery.services.analysis import (
    UNDECODABLE_IMAGE_ERROR,
    UNEXPECTED_ERROR,
    AnalysisService,
)

SERVICE_LOGGER = "gallery.services.analysis"
ANALYZER_ID = "test:model:v1"
SHA = "a" * 64

ANALYSIS = Analysis(tags=("red", "square", "bright", "sample", "test"), description="A red square.")


def png_bytes() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 30)).save(out, format="PNG")
    return out.getvalue()


def make_job(*, attempts: int = 1, path: str = "u/i/original.png") -> AnalysisJob:
    return AnalysisJob(
        image_id=7,
        user_id=UUID(int=1),
        original_path=path,
        content_sha256=SHA,
        attempts=attempts,
    )


@dataclass
class FakeQueue:
    completed: list[dict[str, object]] = field(default_factory=list)
    failed: list[dict[str, object]] = field(default_factory=list)
    complete_error: Exception | None = None
    fail_error: Exception | None = None

    async def claim(self, limit: int) -> list[AnalysisJob]:
        raise NotImplementedError

    async def complete(
        self,
        image_id: int,
        *,
        tags: Sequence[str],
        description: str,
        colors: Sequence[str],
        analyzer_id: str,
    ) -> None:
        if self.complete_error:
            raise self.complete_error
        self.completed.append(
            {
                "image_id": image_id,
                "tags": tuple(tags),
                "description": description,
                "colors": tuple(colors),
                "analyzer_id": analyzer_id,
            }
        )

    async def fail(
        self, image_id: int, *, error: str, colors: Sequence[str], retryable: bool
    ) -> None:
        if self.fail_error:
            raise self.fail_error
        self.failed.append(
            {
                "image_id": image_id,
                "error": error,
                "colors": tuple(colors),
                "retryable": retryable,
            }
        )


@dataclass
class FakeStore:
    objects: dict[str, bytes] = field(default_factory=dict)
    get_error: Exception | None = None

    async def put(self, path: str, data: bytes, content_type: str) -> None:
        self.objects[path] = data

    async def get(self, path: str) -> bytes:
        if self.get_error:
            raise self.get_error
        return self.objects[path]

    async def delete(self, paths: Sequence[str]) -> None:
        for path in paths:
            self.objects.pop(path, None)

    async def signed_urls(self, paths: Sequence[str], ttl_seconds: int) -> dict[str, str]:
        return {}


@dataclass
class ScriptedAnalyzer:
    """Returns `result`, or raises `error` when one is set, and remembers what it was given."""

    result: Analysis = ANALYSIS
    error: Exception | None = None
    calls: list[tuple[int, str]] = field(default_factory=list)

    @property
    def analyzer_id(self) -> str:
        return ANALYZER_ID

    async def analyze(self, image: bytes, mime_type: str) -> Analysis:
        self.calls.append((len(image), mime_type))
        if self.error:
            raise self.error
        return self.result


@dataclass
class FakeCache:
    entries: dict[tuple[str, str], Analysis] = field(default_factory=dict)
    get_error: Exception | None = None
    put_error: Exception | None = None

    async def get(self, content_sha256: str, analyzer_id: str) -> Analysis | None:
        if self.get_error:
            raise self.get_error
        return self.entries.get((content_sha256, analyzer_id))

    async def put(self, content_sha256: str, analyzer_id: str, analysis: Analysis) -> None:
        if self.put_error:
            raise self.put_error
        self.entries[(content_sha256, analyzer_id)] = analysis


@dataclass
class Rig:
    queue: FakeQueue
    store: FakeStore
    analyzer: ScriptedAnalyzer
    cache: FakeCache
    service: AnalysisService


def make_rig(*, image: bytes | None = None, max_attempts: int = 3) -> Rig:
    queue, analyzer, cache = FakeQueue(), ScriptedAnalyzer(), FakeCache()
    store = FakeStore({"u/i/original.png": png_bytes() if image is None else image})
    service = AnalysisService(queue, store, analyzer, cache, max_attempts=max_attempts)
    return Rig(queue, store, analyzer, cache, service)


@pytest.fixture(autouse=True)
def capture_service_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=SERVICE_LOGGER)


def job_line(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    """The one summary line a job writes. It's the only record that carries an `outcome`."""
    lines = [record for record in caplog.records if hasattr(record, "outcome")]
    assert len(lines) == 1, [record.getMessage() for record in caplog.records]
    return lines[0]


def worst_level(caplog: pytest.LogCaptureFixture) -> int:
    return max(record.levelno for record in caplog.records)


# --- outcomes ---------------------------------------------------------------------------------


async def test_success_completes_with_the_colors_and_the_tags() -> None:
    rig = make_rig()

    await rig.service.process(make_job())

    assert rig.queue.completed == [
        {
            "image_id": 7,
            "tags": ANALYSIS.tags,
            "description": ANALYSIS.description,
            "colors": dominant_colors(png_bytes()),
            "analyzer_id": ANALYZER_ID,
        }
    ]
    assert rig.queue.failed == []
    assert rig.analyzer.calls == [(len(png_bytes()), "image/png")]


async def test_success_stores_the_analysis_in_the_cache() -> None:
    rig = make_rig()

    await rig.service.process(make_job())

    assert rig.cache.entries == {(SHA, ANALYZER_ID): ANALYSIS}


async def test_cache_hit_skips_the_analyzer() -> None:
    rig = make_rig()
    cached = Analysis(tags=("cached", "a", "b", "c", "d"), description="From the cache.")
    rig.cache.entries[(SHA, ANALYZER_ID)] = cached

    await rig.service.process(make_job())

    assert rig.analyzer.calls == []
    assert rig.queue.completed[0]["tags"] == cached.tags
    assert rig.queue.completed[0]["description"] == cached.description
    # Colors are always computed locally, cache hit or not.
    assert rig.queue.completed[0]["colors"] == dominant_colors(png_bytes())


async def test_cache_is_keyed_by_analyzer() -> None:
    rig = make_rig()
    rig.cache.entries[(SHA, "another:model:v9")] = Analysis(tags=("x",) * 5, description="Other.")

    await rig.service.process(make_job())

    assert len(rig.analyzer.calls) == 1


async def test_retryable_analyzer_error_fails_the_attempt_and_keeps_the_colors() -> None:
    rig = make_rig()
    rig.analyzer.error = AnalyzerError("The provider is busy", retryable=True)

    await rig.service.process(make_job())

    assert rig.queue.failed == [
        {
            "image_id": 7,
            "error": "The provider is busy",
            "colors": dominant_colors(png_bytes()),
            "retryable": True,
        }
    ]
    assert rig.queue.completed == []
    assert rig.cache.entries == {}


async def test_non_retryable_analyzer_error_fails_for_good() -> None:
    rig = make_rig()
    rig.analyzer.error = AnalyzerError("The API key was rejected", retryable=False)

    await rig.service.process(make_job())

    assert rig.queue.failed == [
        {
            "image_id": 7,
            "error": "The API key was rejected",
            "colors": dominant_colors(png_bytes()),
            "retryable": False,
        }
    ]
    assert rig.queue.completed == []


async def test_corrupt_image_fails_for_good_without_calling_the_analyzer() -> None:
    rig = make_rig(image=b"this is not an image")

    await rig.service.process(make_job())

    assert rig.queue.failed == [
        {
            "image_id": 7,
            "error": UNDECODABLE_IMAGE_ERROR,
            "colors": (),
            "retryable": False,
        }
    ]
    assert UNDECODABLE_IMAGE_ERROR == "Image could not be decoded"
    assert rig.analyzer.calls == []
    assert rig.queue.completed == []


async def test_unexpected_exception_is_retryable_with_a_generic_message() -> None:
    rig = make_rig()
    rig.analyzer.error = ValueError("secret detail that users must not see")

    await rig.service.process(make_job())

    assert rig.queue.failed == [
        {
            "image_id": 7,
            "error": UNEXPECTED_ERROR,
            "colors": dominant_colors(png_bytes()),
            "retryable": True,
        }
    ]
    assert UNEXPECTED_ERROR == "Unexpected error during analysis"
    assert rig.queue.completed == []


async def test_a_storage_failure_is_unexpected_and_has_no_colors_yet() -> None:
    rig = make_rig()
    rig.store.get_error = ConnectionError("storage is down")

    await rig.service.process(make_job())

    assert rig.queue.failed == [
        {"image_id": 7, "error": UNEXPECTED_ERROR, "colors": (), "retryable": True}
    ]
    assert rig.analyzer.calls == []


async def test_a_failing_complete_is_retried_as_an_unexpected_error() -> None:
    rig = make_rig()
    rig.queue.complete_error = ConnectionError("database is down")

    await rig.service.process(make_job())

    assert rig.queue.failed[0]["retryable"] is True
    assert rig.queue.failed[0]["error"] == UNEXPECTED_ERROR


# --- never raises -----------------------------------------------------------------------------


async def test_process_survives_a_failing_fail(caplog: pytest.LogCaptureFixture) -> None:
    rig = make_rig()
    rig.analyzer.error = AnalyzerError("The provider is busy", retryable=True)
    rig.queue.fail_error = ConnectionError("database is down")

    await rig.service.process(make_job())

    recorded = [r for r in caplog.records if "could not record" in r.getMessage()]
    assert len(recorded) == 1
    assert recorded[0].levelno == logging.ERROR
    assert recorded[0].exc_info is not None


async def test_cache_failures_do_not_fail_the_job(caplog: pytest.LogCaptureFixture) -> None:
    rig = make_rig()
    rig.cache.get_error = ConnectionError("cache read failed")
    rig.cache.put_error = ConnectionError("cache write failed")

    await rig.service.process(make_job())

    assert len(rig.queue.completed) == 1
    assert rig.queue.failed == []
    assert len(rig.analyzer.calls) == 1
    # Degraded, not broken: nothing here may reach Sentry.
    assert worst_level(caplog) == logging.WARNING
    assert job_line(caplog).outcome == "done"


# --- log levels and fields --------------------------------------------------------------------


async def test_success_logs_an_info_line(caplog: pytest.LogCaptureFixture) -> None:
    rig = make_rig()

    await rig.service.process(make_job())

    line = job_line(caplog)
    assert line.levelno == logging.INFO
    assert worst_level(caplog) == logging.INFO
    assert line.name == SERVICE_LOGGER
    assert (line.image_id, line.outcome, line.cache_hit) == (7, "done", False)
    assert isinstance(line.duration_ms, int)
    assert line.duration_ms >= 0


async def test_cache_hit_is_logged_without_usage_fields(caplog: pytest.LogCaptureFixture) -> None:
    rig = make_rig()
    rig.cache.entries[(SHA, ANALYZER_ID)] = ANALYSIS

    await rig.service.process(make_job())

    line = job_line(caplog)
    assert line.cache_hit is True
    assert line.outcome == "done"
    for name in ("analyzer_id", "provider_latency_ms", "input_tokens", "output_tokens", "cost_usd"):
        assert not hasattr(line, name)


async def test_usage_fields_are_logged_when_the_provider_reports_them(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = make_rig()
    usage = AnalysisUsage(latency_ms=812, input_tokens=1400, output_tokens=120, cost_usd=0.00042)
    rig.analyzer.result = Analysis(
        tags=ANALYSIS.tags, description=ANALYSIS.description, usage=usage
    )

    await rig.service.process(make_job())

    line = job_line(caplog)
    assert line.analyzer_id == ANALYZER_ID
    assert line.provider_latency_ms == 812
    assert line.input_tokens == 1400
    assert line.output_tokens == 120
    assert line.cost_usd == 0.00042
    assert line.cache_hit is False


async def test_missing_token_counts_are_logged_as_none(caplog: pytest.LogCaptureFixture) -> None:
    rig = make_rig()
    rig.analyzer.result = Analysis(
        tags=ANALYSIS.tags, description=ANALYSIS.description, usage=AnalysisUsage(latency_ms=5)
    )

    await rig.service.process(make_job())

    line = job_line(caplog)
    assert line.provider_latency_ms == 5
    assert (line.input_tokens, line.output_tokens, line.cost_usd) == (None, None, None)


async def test_corrupt_image_is_logged_at_info(caplog: pytest.LogCaptureFixture) -> None:
    rig = make_rig(image=b"this is not an image")

    await rig.service.process(make_job())

    line = job_line(caplog)
    assert line.levelno == logging.INFO
    assert worst_level(caplog) == logging.INFO
    assert (line.outcome, line.cache_hit) == ("failed", False)
    assert line.error == UNDECODABLE_IMAGE_ERROR


async def test_retryable_analyzer_error_is_logged_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = make_rig()
    rig.analyzer.error = AnalyzerError("The provider is busy", retryable=True)

    await rig.service.process(make_job(attempts=1))

    line = job_line(caplog)
    assert line.levelno == logging.WARNING
    assert worst_level(caplog) == logging.WARNING
    assert line.outcome == "retry"
    assert line.error == "The provider is busy"


async def test_the_last_attempt_is_logged_as_failed_even_when_retryable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = make_rig(max_attempts=3)
    rig.analyzer.error = AnalyzerError("The provider is busy", retryable=True)

    await rig.service.process(make_job(attempts=3))

    line = job_line(caplog)
    assert line.outcome == "failed"
    assert line.levelno == logging.WARNING


async def test_non_retryable_analyzer_error_is_logged_at_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = make_rig()
    rig.analyzer.error = AnalyzerError("The API key was rejected", retryable=False)

    await rig.service.process(make_job())

    line = job_line(caplog)
    assert line.levelno == logging.ERROR
    assert line.outcome == "failed"
    assert line.error == "The API key was rejected"


async def test_unexpected_exception_is_logged_with_its_stack_trace(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = make_rig()
    rig.analyzer.error = ValueError("boom")

    await rig.service.process(make_job(attempts=1))

    line = job_line(caplog)
    assert line.levelno == logging.ERROR
    assert line.exc_info is not None
    assert line.exc_info[0] is ValueError
    assert line.outcome == "retry"
    assert line.cache_hit is False


async def test_usage_is_still_logged_when_completing_fails_after_a_paid_analysis(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = make_rig()
    usage = AnalysisUsage(latency_ms=700, input_tokens=1400, output_tokens=120, cost_usd=0.0004)
    rig.analyzer.result = Analysis(
        tags=ANALYSIS.tags, description=ANALYSIS.description, usage=usage
    )
    rig.queue.complete_error = ConnectionError("database is down")

    await rig.service.process(make_job())

    line = job_line(caplog)
    assert line.outcome == "retry"
    assert line.cost_usd == 0.0004
    assert line.input_tokens == 1400
    # The paid result was cached before the failure, so the retry doesn't pay again.
    assert rig.cache.entries == {(SHA, ANALYZER_ID): rig.analyzer.result}


async def test_job_line_carries_the_attempt_number(caplog: pytest.LogCaptureFixture) -> None:
    rig = make_rig()

    await rig.service.process(make_job(attempts=2))

    assert job_line(caplog).attempt == 2
