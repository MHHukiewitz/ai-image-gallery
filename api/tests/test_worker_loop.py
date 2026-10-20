import asyncio
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from uuid import UUID

import pytest

from gallery import worker
from gallery.config import Settings
from gallery.domain.models import AnalysisJob
from gallery.worker import CLAIM_FAILURES_BEFORE_ERROR, work_loop

# Real time for the tests that need it is kept to a few tens of milliseconds.
TICK = 0.01
# A bound on every wait, so a broken loop fails a test instead of hanging the run.
GUARD = 5.0


class Tally(list[int]):
    """A list that lets a test wait until it has grown to a given size."""

    def __init__(self) -> None:
        super().__init__()
        self._waiters: list[tuple[int, asyncio.Event]] = []

    def append(self, item: int) -> None:
        super().append(item)
        for size, event in self._waiters:
            if len(self) >= size:
                event.set()

    async def reached(self, size: int) -> None:
        if len(self) >= size:
            return
        event = asyncio.Event()
        self._waiters.append((size, event))
        await asyncio.wait_for(event.wait(), timeout=GUARD)


def make_job(image_id: int) -> AnalysisJob:
    return AnalysisJob(
        image_id=image_id,
        user_id=UUID(int=1),
        original_path=f"u/{image_id}/original.png",
        content_sha256="a" * 64,
        attempts=1,
    )


class ScriptedQueue:
    """Answers each claim from `script`, one entry per call: jobs to hand out or an error.

    Once the script runs out it keeps answering "no jobs". `on_claim` runs before every answer.
    """

    def __init__(
        self,
        script: list[list[AnalysisJob] | Exception] | None = None,
        on_claim: Callable[[int], None] | None = None,
    ) -> None:
        self._script = list(script or [])
        self._on_claim = on_claim
        self.limits = Tally()

    async def claim(self, limit: int) -> list[AnalysisJob]:
        self.limits.append(limit)
        if self._on_claim:
            self._on_claim(len(self.limits))
        await asyncio.sleep(0)
        answer = self._script.pop(0) if self._script else []
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def complete(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError

    async def fail(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError


class PendingQueue(ScriptedQueue):
    """Hands out up to `limit` jobs from a fixed backlog, like the real queue does."""

    def __init__(self, backlog: list[AnalysisJob]) -> None:
        super().__init__()
        self._backlog = list(backlog)

    async def claim(self, limit: int) -> list[AnalysisJob]:
        self.limits.append(limit)
        await asyncio.sleep(0)
        taken, self._backlog = self._backlog[:limit], self._backlog[limit:]
        return taken


class RecordingRunner:
    def __init__(self, work: Callable[[AnalysisJob], Awaitable[None]] | None = None) -> None:
        self._work = work
        self.started = Tally()
        self.finished = Tally()
        self.active = 0
        self.peak_active = 0

    async def process(self, job: AnalysisJob) -> None:
        self.started.append(job.image_id)
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            if self._work:
                await self._work(job)
        finally:
            self.active -= 1
        self.finished.append(job.image_id)


@pytest.fixture
def recorded_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the loop's sleep with one that only notes how long it was asked to wait."""
    seconds: list[float] = []

    async def record(stop: asyncio.Event, delay: float) -> None:
        seconds.append(delay)
        await asyncio.sleep(0)

    monkeypatch.setattr(worker, "_sleep_or_stop", record)
    return seconds


async def run_loop(
    queue: ScriptedQueue,
    runner: RecordingRunner,
    stop: asyncio.Event,
    *,
    concurrency: int = 4,
    poll_min_seconds: float = TICK,
    poll_max_seconds: float = 4 * TICK,
    drain_timeout_seconds: float = GUARD,
) -> None:
    await asyncio.wait_for(
        work_loop(
            queue,
            runner,
            concurrency=concurrency,
            poll_min_seconds=poll_min_seconds,
            poll_max_seconds=poll_max_seconds,
            stop=stop,
            drain_timeout_seconds=drain_timeout_seconds,
        ),
        timeout=GUARD,
    )


# --- concurrency ------------------------------------------------------------------------------


async def test_never_runs_more_jobs_than_the_concurrency() -> None:
    stop = asyncio.Event()
    jobs = [make_job(i) for i in range(1, 11)]
    queue = PendingQueue(jobs)

    async def work(job: AnalysisJob) -> None:
        await asyncio.sleep(TICK)
        if len(runner.finished) == len(jobs) - 1:
            stop.set()

    runner = RecordingRunner(work)

    await run_loop(queue, runner, stop, concurrency=3)

    assert runner.peak_active == 3
    assert sorted(runner.finished) == [job.image_id for job in jobs]


async def test_claims_only_as_many_jobs_as_there_are_free_slots() -> None:
    stop = asyncio.Event()
    jobs = [make_job(i) for i in range(1, 8)]
    queue = PendingQueue(jobs)

    async def work(job: AnalysisJob) -> None:
        await asyncio.sleep(TICK)
        if len(runner.finished) == len(jobs) - 1:
            stop.set()

    runner = RecordingRunner(work)

    await run_loop(queue, runner, stop, concurrency=3)

    assert queue.limits[0] == 3
    # It never asks for more than it can start, and never asks while it is full.
    assert all(1 <= limit <= 3 for limit in queue.limits)


async def test_a_full_worker_waits_for_a_slot_instead_of_polling(
    recorded_sleeps: list[float],
) -> None:
    stop = asyncio.Event()
    release = asyncio.Event()
    queue = PendingQueue([make_job(1), make_job(2)])

    async def work(job: AnalysisJob) -> None:
        await release.wait()

    runner = RecordingRunner(work)
    loop_task = asyncio.create_task(run_loop(queue, runner, stop, concurrency=2))

    await runner.started.reached(2)
    await asyncio.sleep(TICK)
    # Both slots are taken, so there was exactly one claim and no sleep.
    assert queue.limits == [2]
    assert recorded_sleeps == []

    release.set()
    await queue.limits.reached(2)
    stop.set()
    await loop_task


async def test_a_crashing_runner_does_not_stop_the_loop(caplog: pytest.LogCaptureFixture) -> None:
    stop = asyncio.Event()
    queue = ScriptedQueue(
        [[make_job(1), make_job(2)]], on_claim=lambda n: stop.set() if n > 2 else None
    )

    async def work(job: AnalysisJob) -> None:
        if job.image_id == 1:
            raise RuntimeError("a bug in process")

    runner = RecordingRunner(work)

    await run_loop(queue, runner, stop)

    assert runner.finished == [2]
    crash = [r for r in caplog.records if r.getMessage() == "job runner raised"]
    assert len(crash) == 1
    assert crash[0].levelno == logging.ERROR


# --- adaptive sleep ---------------------------------------------------------------------------


async def test_the_sleep_doubles_up_to_the_maximum_and_resets_when_work_arrives(
    recorded_sleeps: list[float],
) -> None:
    stop = asyncio.Event()
    # Six empty claims, one job, then empty claims until the loop is stopped.
    script: list[list[AnalysisJob] | Exception] = [[] for _ in range(6)]
    script.append([make_job(1)])
    queue = ScriptedQueue(script, on_claim=lambda n: stop.set() if n >= 10 else None)

    await run_loop(queue, RecordingRunner(), stop, poll_min_seconds=1.0, poll_max_seconds=10.0)

    assert recorded_sleeps == [1, 2, 4, 8, 10, 10, 1, 2, 4, 8]


async def test_the_sleep_returns_to_the_minimum_even_after_reaching_the_maximum(
    recorded_sleeps: list[float],
) -> None:
    stop = asyncio.Event()
    script: list[list[AnalysisJob] | Exception] = [[] for _ in range(5)] + [[make_job(1)]]
    queue = ScriptedQueue(script, on_claim=lambda n: stop.set() if n > 6 else None)

    await run_loop(queue, RecordingRunner(), stop, poll_min_seconds=1.0, poll_max_seconds=4.0)

    assert recorded_sleeps[:5] == [1, 2, 4, 4, 4]
    assert recorded_sleeps[5] == 1


async def test_idle_polling_is_slower_than_polling_with_work() -> None:
    """The same thing with real clocks and no patching: later gaps are longer, then reset."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    times: list[float] = []
    script: list[list[AnalysisJob] | Exception] = [[] for _ in range(4)] + [[make_job(1)]]

    def on_claim(count: int) -> None:
        times.append(loop.time())
        if count == 8:
            stop.set()

    queue = ScriptedQueue(script, on_claim=on_claim)

    await run_loop(
        queue,
        RecordingRunner(),
        stop,
        poll_min_seconds=0.05,
        poll_max_seconds=0.4,
    )

    gaps = [later - earlier for earlier, later in zip(times, times[1:], strict=False)]
    # A sleep never ends early, so the lower bounds are safe on a slow machine: 0.05, 0.1, 0.2, 0.4.
    assert gaps[0] >= 0.045
    assert gaps[1] >= 0.095
    assert gaps[2] >= 0.195
    assert gaps[3] >= 0.395
    # The claim after the job came after only the minimum sleep, far below the 0.8 s it would
    # have waited without the reset.
    assert gaps[4] < 0.3


# --- claim failures ---------------------------------------------------------------------------


async def test_a_failed_claim_backs_off_to_the_maximum_and_carries_on(
    recorded_sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    stop = asyncio.Event()
    queue = ScriptedQueue(
        [ConnectionError("network is down"), [make_job(1)]],
        on_claim=lambda n: stop.set() if n > 3 else None,
    )
    runner = RecordingRunner()

    await run_loop(queue, runner, stop, poll_min_seconds=1.0, poll_max_seconds=10.0)

    assert recorded_sleeps[0] == 10
    assert runner.finished == [1]
    failure = [r for r in caplog.records if r.getMessage().startswith("could not claim")]
    assert [r.levelno for r in failure] == [logging.WARNING]
    assert failure[0].exc_info is not None
    assert failure[0].exc_info[0] is ConnectionError


async def test_an_unbroken_run_of_failed_claims_reaches_error_once(
    recorded_sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    stop = asyncio.Event()
    failures = CLAIM_FAILURES_BEFORE_ERROR + 3
    script: list[list[AnalysisJob] | Exception] = [ConnectionError("down")] * failures
    queue = ScriptedQueue(script, on_claim=lambda n: stop.set() if n > failures else None)

    await run_loop(queue, RecordingRunner(), stop)

    levels = [r.levelno for r in caplog.records if r.getMessage().startswith("could not claim")]
    assert len(levels) == failures
    assert levels.count(logging.ERROR) == 1
    assert levels[CLAIM_FAILURES_BEFORE_ERROR - 1] == logging.ERROR


async def test_a_successful_claim_resets_the_failure_count(
    recorded_sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    stop = asyncio.Event()
    almost = CLAIM_FAILURES_BEFORE_ERROR - 1
    script: list[list[AnalysisJob] | Exception] = [ConnectionError("down")] * almost
    script.append([])
    script += [ConnectionError("down")] * almost
    queue = ScriptedQueue(script, on_claim=lambda n: stop.set() if n > len(script) else None)

    await run_loop(queue, RecordingRunner(), stop)

    assert all(r.levelno < logging.ERROR for r in caplog.records)


# --- shutdown ---------------------------------------------------------------------------------


async def test_shutdown_stops_claiming_and_drains_running_jobs() -> None:
    stop = asyncio.Event()
    release = asyncio.Event()
    queue = PendingQueue([make_job(1), make_job(2), make_job(3)])

    async def work(job: AnalysisJob) -> None:
        await release.wait()

    runner = RecordingRunner(work)
    loop_task = asyncio.create_task(run_loop(queue, runner, stop, concurrency=2))
    await runner.started.reached(2)

    stop.set()
    await asyncio.sleep(5 * TICK)
    # Still waiting for the two running jobs.
    assert not loop_task.done()
    assert runner.finished == []

    release.set()
    await loop_task

    assert sorted(runner.finished) == [1, 2]
    # Job 3 was never claimed: the loop was full when the stop came.
    assert runner.started == [1, 2]
    assert queue.limits == [2]


async def test_shutdown_while_idle_returns_without_waiting_out_the_sleep() -> None:
    stop = asyncio.Event()
    queue = ScriptedQueue()
    loop_task = asyncio.create_task(
        run_loop(queue, RecordingRunner(), stop, poll_min_seconds=60.0, poll_max_seconds=60.0)
    )
    await queue.limits.reached(1)

    stop.set()

    await asyncio.wait_for(loop_task, timeout=1.0)


async def test_nothing_is_claimed_after_the_stop() -> None:
    stop = asyncio.Event()
    stop.set()
    queue = ScriptedQueue([[make_job(1)]])

    await run_loop(queue, RecordingRunner(), stop)

    assert queue.limits == []


async def test_jobs_still_running_after_the_drain_timeout_are_cancelled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = asyncio.Event()
    cancelled = asyncio.Event()
    queue = PendingQueue([make_job(1)])

    async def work(job: AnalysisJob) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    runner = RecordingRunner(work)
    loop_task = asyncio.create_task(run_loop(queue, runner, stop, drain_timeout_seconds=2 * TICK))
    await runner.started.reached(1)

    stop.set()
    await loop_task

    assert cancelled.is_set()
    abandoned = [r for r in caplog.records if r.getMessage().startswith("drain timed out")]
    assert len(abandoned) == 1
    assert abandoned[0].levelno == logging.WARNING


@pytest.mark.parametrize("received", [signal.SIGTERM, signal.SIGINT])
async def test_sigterm_and_sigint_set_the_stop_event(
    received: signal.Signals, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="gallery.worker")
    stop = asyncio.Event()

    with worker._stop_on_signals(stop):
        assert not stop.is_set()
        os.kill(os.getpid(), received)
        await asyncio.wait_for(stop.wait(), timeout=GUARD)

    assert any(r.getMessage() == "shutdown requested" for r in caplog.records)


async def test_a_signal_drains_a_running_loop() -> None:
    stop = asyncio.Event()
    release = asyncio.Event()
    queue = PendingQueue([make_job(1)])

    async def work(job: AnalysisJob) -> None:
        await release.wait()

    runner = RecordingRunner(work)
    with worker._stop_on_signals(stop):
        loop_task = asyncio.create_task(run_loop(queue, runner, stop))
        await runner.started.reached(1)

        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stop.wait(), timeout=GUARD)
        await asyncio.sleep(2 * TICK)
        assert not loop_task.done()

        release.set()
        await loop_task

    assert runner.finished == [1]


# --- entrypoint -------------------------------------------------------------------------------


def test_main_sets_up_observability_before_running(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    calls: list[str] = []

    def fake_init(received: Settings, component: str) -> None:
        calls.append(f"init_observability:{component}")

    async def fake_run(received: Settings, stop: asyncio.Event | None = None) -> None:
        calls.append("run")

    monkeypatch.setattr(worker, "init_observability", fake_init)
    monkeypatch.setattr(worker, "run", fake_run)

    worker.main()

    assert calls == ["init_observability:worker", "run"]
