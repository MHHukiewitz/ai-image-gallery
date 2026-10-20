import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

import pytest
from PIL import Image

from gallery.domain.colors import dominant_colors
from gallery.domain.errors import AnalyzerError, TagVerificationError
from gallery.domain.models import Analysis, AnalysisJob, AnalysisUsage, TagVerdict
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


# --- the optional tag verifier ----------------------------------------------------------------

EIGHT_TAGS = Analysis(
    tags=("house", "red house", "sun", "hill", "tree", "road", "cat", "beach"),
    description="A red house on a hill.",
)


@dataclass
class ScriptedVerifier:
    """Returns `verdict`, or raises `error` when one is set, and remembers what it was given."""

    verdict: TagVerdict = field(default_factory=lambda: TagVerdict(probabilities={}))
    error: Exception | None = None
    calls: list[tuple[int, str, tuple[str, ...]]] = field(default_factory=list)

    async def verify(self, image: bytes, mime_type: str, tags: Sequence[str]) -> TagVerdict:
        self.calls.append((len(image), mime_type, tuple(tags)))
        if self.error:
            raise self.error
        return self.verdict


def make_verified_rig(verifier: ScriptedVerifier, *, min_tag_probability: float = 0.5) -> Rig:
    """A rig whose analyzer returns eight tags and whose service has `verifier`."""
    rig = make_rig()
    rig.analyzer.result = EIGHT_TAGS
    rig.service = AnalysisService(
        rig.queue,
        rig.store,
        rig.analyzer,
        rig.cache,
        verifier=verifier,
        min_tag_probability=min_tag_probability,
    )
    return rig


def stored_tags(rig: Rig) -> tuple[str, ...]:
    (completed,) = rig.queue.completed
    tags = completed["tags"]
    assert isinstance(tags, tuple)
    return tags


async def test_the_verifier_runs_after_the_analyzer_and_its_verdict_decides_the_tags() -> None:
    verdict = TagVerdict(
        probabilities={"house": 0.98, "red house": 0.97, "cat": 0.03, "beach": 0.02},
        merges=(("red house", "house"),),
    )
    verifier = ScriptedVerifier(verdict)
    rig = make_verified_rig(verifier)

    await rig.service.process(make_job())

    assert stored_tags(rig) == ("red house", "sun", "hill", "tree", "road")
    assert verifier.calls == [(len(png_bytes()), "image/png", EIGHT_TAGS.tags)]
    assert len(rig.analyzer.calls) == 1
    assert rig.queue.failed == []


async def test_the_cache_keeps_the_analyzers_tags_not_the_verified_ones() -> None:
    verifier = ScriptedVerifier(TagVerdict(probabilities={"cat": 0.01, "beach": 0.01}))
    rig = make_verified_rig(verifier)

    await rig.service.process(make_job())

    assert "cat" not in stored_tags(rig)
    assert rig.cache.entries == {(SHA, ANALYZER_ID): EIGHT_TAGS}


async def test_the_verifier_also_runs_on_a_cache_hit() -> None:
    verifier = ScriptedVerifier(TagVerdict(probabilities={"cat": 0.01, "beach": 0.01}))
    rig = make_verified_rig(verifier)
    rig.cache.entries[(SHA, ANALYZER_ID)] = EIGHT_TAGS

    await rig.service.process(make_job())

    assert rig.analyzer.calls == []
    assert verifier.calls == [(len(png_bytes()), "image/png", EIGHT_TAGS.tags)]
    assert stored_tags(rig) == ("house", "red house", "sun", "hill", "tree", "road")


async def test_the_verifier_cannot_leave_fewer_than_five_tags() -> None:
    verifier = ScriptedVerifier(TagVerdict(probabilities={tag: 0.01 for tag in EIGHT_TAGS.tags}))
    rig = make_verified_rig(verifier)

    await rig.service.process(make_job())

    assert len(stored_tags(rig)) == 5


async def test_the_threshold_comes_from_the_service_setting() -> None:
    probabilities = dict.fromkeys(EIGHT_TAGS.tags, 0.8) | {"cat": 0.6, "beach": 0.7}

    lenient = make_verified_rig(
        ScriptedVerifier(TagVerdict(probabilities)), min_tag_probability=0.5
    )
    strict = make_verified_rig(
        ScriptedVerifier(TagVerdict(probabilities)), min_tag_probability=0.75
    )
    await lenient.service.process(make_job())
    await strict.service.process(make_job())

    assert stored_tags(lenient) == EIGHT_TAGS.tags
    assert stored_tags(strict) == ("house", "red house", "sun", "hill", "tree", "road")


async def test_the_verifier_is_not_called_when_the_analyzer_fails() -> None:
    verifier = ScriptedVerifier()
    rig = make_verified_rig(verifier)
    rig.analyzer.error = AnalyzerError("The provider is busy", retryable=True)

    await rig.service.process(make_job())

    assert verifier.calls == []
    assert rig.queue.completed == []


async def test_a_verification_error_keeps_the_analyzers_tags_and_the_job_completes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    verifier = ScriptedVerifier(error=TagVerificationError("Jev did not answer within 5.0 seconds"))
    rig = make_verified_rig(verifier)

    await rig.service.process(make_job())

    assert stored_tags(rig) == EIGHT_TAGS.tags
    assert rig.queue.failed == []
    assert job_line(caplog).outcome == "done"
    # A warning: it must not reach Sentry, and it says why.
    assert worst_level(caplog) == logging.WARNING
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.image_id == 7
    assert "5.0 seconds" in warning.error


async def test_an_unexpected_verifier_exception_keeps_the_tags_and_is_logged_as_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    verifier = ScriptedVerifier(error=ValueError("bug in the verifier"))
    rig = make_verified_rig(verifier)

    await rig.service.process(make_job())

    assert stored_tags(rig) == EIGHT_TAGS.tags
    assert rig.queue.failed == []
    line = job_line(caplog)
    assert (line.outcome, line.levelno) == ("done", logging.INFO)
    (crash,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert crash.exc_info is not None
    assert crash.exc_info[0] is ValueError


async def test_the_job_line_reports_the_verifier(caplog: pytest.LogCaptureFixture) -> None:
    usage = AnalysisUsage(latency_ms=597, input_tokens=2764, output_tokens=13, cost_usd=0.000116)
    verdict = TagVerdict(
        probabilities={"cat": 0.03, "beach": 0.02},
        merges=(("red house", "house"),),
        usage=usage,
    )
    rig = make_verified_rig(ScriptedVerifier(verdict))

    await rig.service.process(make_job())

    line = job_line(caplog)
    assert isinstance(line.verifier_latency_ms, int)
    assert line.verifier_latency_ms >= 0
    assert line.verifier_input_tokens == 2764
    assert line.verifier_cost_usd == 0.000116
    assert line.tags_dropped == 2
    assert line.tags_merged == 1


async def test_a_failed_verification_is_logged_without_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = make_verified_rig(ScriptedVerifier(error=TagVerificationError("Jev is down")))

    await rig.service.process(make_job())

    line = job_line(caplog)
    assert isinstance(line.verifier_latency_ms, int)
    for name in ("verifier_input_tokens", "verifier_cost_usd", "tags_dropped", "tags_merged"):
        assert not hasattr(line, name)


async def test_the_verifier_fields_are_logged_when_completing_fails_afterwards(
    caplog: pytest.LogCaptureFixture,
) -> None:
    usage = AnalysisUsage(latency_ms=600, input_tokens=2764)
    rig = make_verified_rig(ScriptedVerifier(TagVerdict({"cat": 0.01}, usage=usage)))
    rig.queue.complete_error = ConnectionError("database is down")

    await rig.service.process(make_job())

    line = job_line(caplog)
    assert line.outcome == "retry"
    assert line.verifier_input_tokens == 2764
    assert line.tags_dropped == 1


async def test_without_a_verifier_nothing_changes(caplog: pytest.LogCaptureFixture) -> None:
    rig = make_rig()
    rig.analyzer.result = EIGHT_TAGS

    await rig.service.process(make_job())

    assert stored_tags(rig) == EIGHT_TAGS.tags
    line = job_line(caplog)
    for name in (
        "verifier_latency_ms",
        "verifier_input_tokens",
        "verifier_cost_usd",
        "tags_dropped",
        "tags_merged",
    ):
        assert not hasattr(line, name)
