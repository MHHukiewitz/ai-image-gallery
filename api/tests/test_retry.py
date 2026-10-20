"""The shared retry loop and the classification of PostgREST failures. No network."""

import logging
from collections.abc import Callable

import httpx
import pytest
from postgrest import APIError

from gallery.adapters.retry import RetryPolicy, retry_transient, transient_database_reason

logger = logging.getLogger("test_retry")
POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=0.2, cap_delay_seconds=0.5)


class Pauses:
    """Stands in for `asyncio.sleep` and `random.uniform`, and keeps what they were asked."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self.ranges: list[tuple[float, float]] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def uniform(self, low: float, high: float) -> float:
        self.ranges.append((low, high))
        return high


def always(reason: str) -> Callable[[Exception], str | None]:
    return lambda error: reason


def never(error: Exception) -> str | None:
    return None


async def run(
    call: Callable[[], str],
    pauses: Pauses,
    *,
    transient_reason: Callable[[Exception], str | None],
    policy: RetryPolicy = POLICY,
) -> str:
    async def wrapped() -> str:
        return call()

    return await retry_transient(
        wrapped,
        operation="op",
        policy=policy,
        transient_reason=transient_reason,
        logger=logger,
        message="call failed, retrying",
        sleep=pauses.sleep,
        uniform=pauses.uniform,
    )


class Flaky:
    """Fails with `error` for the first `failures` calls, then answers."""

    def __init__(self, failures: int, error: Exception | None = None) -> None:
        self.calls = 0
        self._failures = failures
        self._error = error or RuntimeError("boom")

    def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self._failures:
            raise self._error
        return "ok"


# --- retry_transient --------------------------------------------------------------------------


async def test_a_call_that_works_is_made_once_and_never_waits() -> None:
    pauses = Pauses()
    flaky = Flaky(0)

    assert await run(flaky, pauses, transient_reason=always("x")) == "ok"
    assert flaky.calls == 1
    assert pauses.sleeps == []


async def test_a_transient_failure_is_retried_until_it_works() -> None:
    pauses = Pauses()
    flaky = Flaky(2)

    assert await run(flaky, pauses, transient_reason=always("x")) == "ok"
    assert flaky.calls == 3
    assert len(pauses.sleeps) == 2


async def test_the_last_error_is_raised_after_the_maximum_attempts() -> None:
    pauses = Pauses()
    flaky = Flaky(10)

    with pytest.raises(RuntimeError, match="boom"):
        await run(flaky, pauses, transient_reason=always("x"))

    assert flaky.calls == POLICY.max_attempts
    assert len(pauses.sleeps) == POLICY.max_attempts - 1


async def test_an_error_that_is_not_transient_is_raised_at_once() -> None:
    pauses = Pauses()
    flaky = Flaky(1)

    with pytest.raises(RuntimeError):
        await run(flaky, pauses, transient_reason=never)

    assert flaky.calls == 1
    assert pauses.sleeps == []


async def test_the_pause_is_drawn_from_a_window_that_doubles_up_to_the_cap() -> None:
    pauses = Pauses()
    policy = RetryPolicy(max_attempts=5, base_delay_seconds=0.2, cap_delay_seconds=0.5)

    with pytest.raises(RuntimeError):
        await run(Flaky(10), pauses, transient_reason=always("x"), policy=policy)

    assert pauses.ranges == [(0, 0.2), (0, 0.4), (0, 0.5), (0, 0.5)]
    # The pause is whatever the draw returned, not the ceiling computed here.
    assert pauses.sleeps == [0.2, 0.4, 0.5, 0.5]


async def test_each_retry_is_logged_as_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    pauses = Pauses()

    with caplog.at_level(logging.WARNING, logger=logger.name):
        await run(Flaky(2), pauses, transient_reason=always("ReadTimeout"))

    first, second = caplog.records
    assert first.levelno == logging.WARNING
    assert first.getMessage() == "call failed, retrying"
    assert (first.operation, first.status, first.attempt) == ("op", "ReadTimeout", 1)
    assert (second.operation, second.status, second.attempt) == ("op", "ReadTimeout", 2)


# --- which database failures are worth a retry ------------------------------------------------


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (httpx.ReadTimeout("no answer in 8 s"), "ReadTimeout"),
        (httpx.ConnectTimeout("no connection"), "ConnectTimeout"),
        (httpx.PoolTimeout("no free connection"), "PoolTimeout"),
        (httpx.ConnectError("refused"), "ConnectError"),
        (httpx.ReadError("reset"), "ReadError"),
        # A reply that is not PostgREST's own error: the SDK keeps the HTTP status as an int.
        (APIError({"message": "JSON could not be generated", "code": 429}), "429"),
        (APIError({"message": "JSON could not be generated", "code": 502}), "502"),
        (APIError({"message": "JSON could not be generated", "code": 503}), "503"),
        (APIError({"message": "JSON could not be generated", "code": 504}), "504"),
        (APIError({"message": "x", "code": "500"}), "500"),
        # PostgREST's "the database is not reachable right now" errors.
        (APIError({"message": "schema cache", "code": "PGRST002"}), "PGRST002"),
        (APIError({"message": "pool", "code": "PGRST003"}), "PGRST003"),
        (APIError({"message": "connect", "code": "PGRST000"}), "PGRST000"),
        # Postgres running out of connections, or losing one.
        (APIError({"message": "too many connections", "code": "53300"}), "53300"),
        (APIError({"message": "connection failure", "code": "08006"}), "08006"),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else str(value),
)
def test_failures_that_a_moment_later_may_fix_are_transient(error: Exception, reason: str) -> None:
    assert transient_database_reason(error) == reason


@pytest.mark.parametrize(
    "error",
    [
        APIError({"message": "permission denied", "code": "42501"}),
        APIError({"message": "duplicate key", "code": "23505"}),
        APIError({"message": "no rows", "code": "PGRST116"}),
        APIError({"message": "raised by the function", "code": "P0001"}),
        APIError({"message": "bad request", "code": 400}),
        APIError({"message": "not found", "code": 404}),
        # Three digits that are not a transient status are a status, not a SQLSTATE class.
        APIError({"message": "x", "code": "530"}),
        APIError({"message": "no code at all"}),
        ValueError("a bug"),
        RuntimeError("another bug"),
    ],
    ids=repr,
)
def test_failures_a_retry_cannot_fix_are_not_transient(error: Exception) -> None:
    assert transient_database_reason(error) is None
