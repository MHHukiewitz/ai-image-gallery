import asyncio
import logging
import os
import signal
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from PIL import Image

from gallery import worker
from gallery.config import Settings
from gallery.domain.models import Job
from gallery.worker import CLAIM_FAILURES_BEFORE_ERROR, JobHandler, work_loop

# Real time for the tests that need it is kept to a few tens of milliseconds.
TICK = 0.01
# A bound on every wait, so a broken loop fails a test instead of hanging the run.
GUARD = 5.0
KIND = "analyze_image"


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


def make_job(job_id: int, kind: str = KIND) -> Job:
    return Job(id=job_id, kind=kind, image_id=job_id, user_id=UUID(int=1), attempts=1)


class ScriptedQueue:
    """Answers each claim from `script`, one entry per call: jobs to hand out or an error.

    Once the script runs out it keeps answering "no jobs". `on_claim` runs before every answer.
    The loop only ever fails a job itself when it has no handler for its kind; `failed` keeps
    those calls.
    """

    def __init__(
        self,
        script: list[list[Job] | Exception] | None = None,
        on_claim: Callable[[int], None] | None = None,
    ) -> None:
        self._script = list(script or [])
        self._on_claim = on_claim
        self.limits = Tally()
        self.kinds: list[list[str]] = []
        self.failed: list[tuple[int, str, bool]] = []
        self.fail_error: Exception | None = None

    async def claim(self, kinds: list[str], limit: int) -> list[Job]:
        self.kinds.append(list(kinds))
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

    async def fail(self, job_id: int, *, error: str, retryable: bool) -> None:
        if self.fail_error:
            raise self.fail_error
        self.failed.append((job_id, error, retryable))


class GatedQueue(ScriptedQueue):
    """A scripted queue that holds claim number `hold_claim` until the test opens `gate`."""

    def __init__(self, script: list[list[Job] | Exception], *, hold_claim: int) -> None:
        super().__init__(script)
        self.gate = asyncio.Event()
        self._hold_claim = hold_claim

    async def claim(self, kinds: list[str], limit: int) -> list[Job]:
        jobs = await super().claim(kinds, limit)
        if len(self.limits) == self._hold_claim:
            await self.gate.wait()
        return jobs


class PendingQueue(ScriptedQueue):
    """Hands out up to `limit` jobs from a fixed backlog, like the real queue does."""

    def __init__(self, backlog: list[Job]) -> None:
        super().__init__()
        self._backlog = list(backlog)

    async def claim(self, kinds: list[str], limit: int) -> list[Job]:
        self.limits.append(limit)
        await asyncio.sleep(0)
        taken, self._backlog = self._backlog[:limit], self._backlog[limit:]
        return taken


class RecordingRunner:
    def __init__(self, work: Callable[[Job], Awaitable[None]] | None = None) -> None:
        self._work = work
        self.started = Tally()
        self.finished = Tally()
        self.active = 0
        self.peak_active = 0

    async def process(self, job: Job) -> None:
        self.started.append(job.id)
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            if self._work:
                await self._work(job)
        finally:
            self.active -= 1
        self.finished.append(job.id)


@pytest.fixture
def recorded_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the loop's sleep with one that only notes how long it was asked to wait."""
    seconds: list[float] = []

    async def record(
        stop: asyncio.Event, delay: float, *, wake: asyncio.Event | None = None
    ) -> None:
        seconds.append(delay)
        await asyncio.sleep(0)

    monkeypatch.setattr(worker, "_sleep_or_stop", record)
    return seconds


async def run_loop(
    queue: ScriptedQueue,
    runner: RecordingRunner,
    stop: asyncio.Event,
    *,
    handlers: Mapping[str, JobHandler] | None = None,
    concurrency: int = 4,
    poll_min_seconds: float = TICK,
    poll_max_seconds: float = 4 * TICK,
    drain_timeout_seconds: float = GUARD,
) -> None:
    """Run the loop with `runner` as the handler of KIND, unless `handlers` are given."""
    await asyncio.wait_for(
        work_loop(
            queue,
            handlers if handlers is not None else {KIND: runner.process},
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

    async def work(job: Job) -> None:
        await asyncio.sleep(TICK)
        if len(runner.finished) == len(jobs) - 1:
            stop.set()

    runner = RecordingRunner(work)

    await run_loop(queue, runner, stop, concurrency=3)

    assert runner.peak_active == 3
    assert sorted(runner.finished) == [job.id for job in jobs]


async def test_claims_only_as_many_jobs_as_there_are_free_slots() -> None:
    stop = asyncio.Event()
    jobs = [make_job(i) for i in range(1, 8)]
    queue = PendingQueue(jobs)

    async def work(job: Job) -> None:
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

    async def work(job: Job) -> None:
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

    async def work(job: Job) -> None:
        if job.id == 1:
            raise RuntimeError("a bug in process")

    runner = RecordingRunner(work)

    await run_loop(queue, runner, stop)

    assert runner.finished == [2]
    crash = [r for r in caplog.records if r.getMessage() == "job handler raised"]
    assert len(crash) == 1
    assert crash[0].levelno == logging.ERROR


# --- dispatch by kind -------------------------------------------------------------------------


async def test_each_job_runs_through_the_handler_of_its_kind() -> None:
    stop = asyncio.Event()
    queue = ScriptedQueue(
        [[make_job(1, "analyze_image"), make_job(2, "re_embed"), make_job(3, "analyze_image")]],
        on_claim=lambda n: stop.set() if n > 1 else None,
    )
    analyses, embeddings = RecordingRunner(), RecordingRunner()

    await run_loop(
        queue,
        analyses,
        stop,
        handlers={"analyze_image": analyses.process, "re_embed": embeddings.process},
    )

    assert sorted(analyses.finished) == [1, 3]
    assert embeddings.finished == [2]
    assert queue.failed == []


async def test_the_claim_asks_only_for_the_kinds_that_have_a_handler() -> None:
    stop = asyncio.Event()
    queue = ScriptedQueue(on_claim=lambda n: stop.set())
    runner = RecordingRunner()

    await run_loop(
        queue, runner, stop, handlers={"analyze_image": runner.process, "re_embed": runner.process}
    )

    assert queue.kinds == [["analyze_image", "re_embed"]]


async def test_an_unknown_kind_fails_its_job_and_the_loop_carries_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = asyncio.Event()
    queue = ScriptedQueue(
        [[make_job(1, "mystery"), make_job(2)], [make_job(3)]],
        on_claim=lambda n: stop.set() if n > 2 else None,
    )
    runner = RecordingRunner()

    await run_loop(queue, runner, stop)

    # Final: retrying can't help a worker that has no handler for the kind.
    assert queue.failed == [(1, "No handler for job kind 'mystery'", False)]
    assert runner.started == [2, 3]
    assert sorted(runner.finished) == [2, 3]
    unknown = [r for r in caplog.records if r.getMessage().startswith("no handler")]
    assert [r.levelno for r in unknown] == [logging.ERROR]
    assert (unknown[0].job_id, unknown[0].kind) == (1, "mystery")


async def test_a_failure_to_fail_an_unknown_kind_does_not_stop_the_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = asyncio.Event()
    queue = ScriptedQueue(
        [[make_job(1, "mystery"), make_job(2)]], on_claim=lambda n: stop.set() if n > 1 else None
    )
    queue.fail_error = ConnectionError("database is down")
    runner = RecordingRunner()

    await run_loop(queue, runner, stop)

    assert runner.finished == [2]
    crash = [r for r in caplog.records if r.getMessage() == "job handler raised"]
    assert [(r.levelno, r.job_id) for r in crash] == [(logging.ERROR, 1)]


# --- adaptive sleep ---------------------------------------------------------------------------


async def test_the_sleep_doubles_up_to_the_maximum_and_resets_when_work_arrives(
    recorded_sleeps: list[float],
) -> None:
    stop = asyncio.Event()
    # Six empty claims, one job, then empty claims until the loop is stopped.
    script: list[list[Job] | Exception] = [[] for _ in range(6)]
    script.append([make_job(1)])
    queue = ScriptedQueue(script, on_claim=lambda n: stop.set() if n >= 10 else None)

    await run_loop(queue, RecordingRunner(), stop, poll_min_seconds=1.0, poll_max_seconds=10.0)

    assert recorded_sleeps == [1, 2, 4, 8, 10, 10, 1, 2, 4, 8]


async def test_the_sleep_returns_to_the_minimum_even_after_reaching_the_maximum(
    recorded_sleeps: list[float],
) -> None:
    stop = asyncio.Event()
    script: list[list[Job] | Exception] = [[] for _ in range(5)] + [[make_job(1)]]
    queue = ScriptedQueue(script, on_claim=lambda n: stop.set() if n > 6 else None)

    await run_loop(queue, RecordingRunner(), stop, poll_min_seconds=1.0, poll_max_seconds=4.0)

    assert recorded_sleeps[:5] == [1, 2, 4, 4, 4]
    assert recorded_sleeps[5] == 1


async def test_idle_polling_is_slower_than_polling_with_work() -> None:
    """The same thing with real clocks and no patching: later gaps are longer, then reset."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    times: list[float] = []
    script: list[list[Job] | Exception] = [[] for _ in range(4)] + [[make_job(1)]]

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


async def test_the_default_poll_settings_never_sleep_more_than_three_seconds(
    recorded_sleeps: list[float],
) -> None:
    settings = Settings()
    stop = asyncio.Event()
    queue = ScriptedQueue(on_claim=lambda n: stop.set() if n >= 8 else None)

    await run_loop(
        queue,
        RecordingRunner(),
        stop,
        poll_min_seconds=settings.WORKER_POLL_MIN_SECONDS,
        poll_max_seconds=settings.WORKER_POLL_MAX_SECONDS,
    )

    # An idle queue is polled every second, then every 2 s, then at most every 3 s.
    assert recorded_sleeps == [1, 2, 3, 3, 3, 3, 3, 3]


# --- waking when a job finishes ---------------------------------------------------------------
# These use a poll interval of a minute. A loop that only wakes on the clock would not claim again
# within the test's 5 s guard, so a second claim can only come from the job finishing.


async def test_a_finished_job_cuts_the_idle_sleep_short() -> None:
    stop = asyncio.Event()
    release = asyncio.Event()
    queue = ScriptedQueue([[make_job(1)]])

    async def work(job: Job) -> None:
        await release.wait()

    runner = RecordingRunner(work)
    loop_task = asyncio.create_task(
        run_loop(queue, runner, stop, concurrency=2, poll_min_seconds=60.0, poll_max_seconds=60.0)
    )
    await runner.started.reached(1)
    await asyncio.sleep(TICK)
    # One job is running, one slot is free, and the loop is asleep for a minute.
    assert queue.limits == [2]

    release.set()
    await queue.limits.reached(2)

    # It claims at once, and for both slots again: the job that finished freed its own.
    assert queue.limits == [2, 2]
    stop.set()
    await loop_task


async def test_the_claim_after_a_finished_job_asks_for_the_slot_it_freed() -> None:
    stop = asyncio.Event()
    release = asyncio.Event()
    # Two jobs fill the worker, so it waits for a slot. Then the queue holds one more.
    queue = ScriptedQueue([[make_job(1), make_job(2)], [make_job(3)]])

    async def work(job: Job) -> None:
        if job.id in (1, 2):
            await release.wait()

    runner = RecordingRunner(work)
    loop_task = asyncio.create_task(
        run_loop(queue, runner, stop, concurrency=2, poll_min_seconds=60.0, poll_max_seconds=60.0)
    )
    await runner.started.reached(2)

    release.set()
    await runner.finished.reached(3)

    assert queue.limits == [2, 2]
    stop.set()
    await loop_task


async def test_a_wake_up_that_finds_no_work_goes_back_to_sleep() -> None:
    stop = asyncio.Event()
    release = asyncio.Event()
    queue = ScriptedQueue([[make_job(1)]])

    async def work(job: Job) -> None:
        await release.wait()

    runner = RecordingRunner(work)
    loop_task = asyncio.create_task(
        run_loop(queue, runner, stop, poll_min_seconds=60.0, poll_max_seconds=60.0)
    )
    await runner.started.reached(1)

    release.set()
    await queue.limits.reached(2)
    await asyncio.sleep(10 * TICK)

    # One claim for the wake-up, and then it sleeps again instead of spinning on the flag.
    assert len(queue.limits) == 2
    stop.set()
    await loop_task


async def test_a_job_that_finishes_during_a_claim_wakes_the_next_sleep() -> None:
    stop = asyncio.Event()
    release_second_job = asyncio.Event()
    queue = GatedQueue([[make_job(1), make_job(2)]], hold_claim=2)

    async def work(job: Job) -> None:
        if job.id == 2:
            await release_second_job.wait()

    runner = RecordingRunner(work)
    loop_task = asyncio.create_task(
        run_loop(queue, runner, stop, concurrency=3, poll_min_seconds=60.0, poll_max_seconds=60.0)
    )
    # The first job finishes at once and wakes the loop, whose second claim is held at the gate.
    await queue.limits.reached(2)

    release_second_job.set()
    await runner.finished.reached(2)
    queue.gate.set()

    # The second job ended while the claim was in flight. That must not be lost: the loop claims
    # a third time without sleeping.
    await queue.limits.reached(3)
    stop.set()
    await loop_task


async def test_the_idle_sleep_returns_early_when_woken() -> None:
    stop = asyncio.Event()
    wake = asyncio.Event()
    wake.set()

    await asyncio.wait_for(worker._sleep_or_stop(stop, 60.0, wake=wake), timeout=1.0)


async def test_the_idle_sleep_returns_early_when_stopped() -> None:
    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(worker._sleep_or_stop(stop, 60.0, wake=asyncio.Event()), timeout=1.0)


async def test_the_idle_sleep_lasts_the_full_time_without_a_wake_up() -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()

    await worker._sleep_or_stop(asyncio.Event(), 0.05, wake=asyncio.Event())

    assert loop.time() - started >= 0.045


async def test_the_idle_sleep_leaves_no_waiting_tasks_behind() -> None:
    wake = asyncio.Event()
    wake.set()

    await worker._sleep_or_stop(asyncio.Event(), 60.0, wake=wake)
    await asyncio.sleep(0)

    assert asyncio.all_tasks() == {asyncio.current_task()}


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
    script: list[list[Job] | Exception] = [ConnectionError("down")] * failures
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
    script: list[list[Job] | Exception] = [ConnectionError("down")] * almost
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

    async def work(job: Job) -> None:
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

    async def work(job: Job) -> None:
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

    async def work(job: Job) -> None:
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


# --- wiring -----------------------------------------------------------------------------------


class RecordingService:
    """Stands in for AnalysisService and remembers how the worker built it."""

    instances: list["RecordingService"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.kwargs = kwargs
        RecordingService.instances.append(self)

    async def process(self, job: object) -> None:
        raise NotImplementedError


@pytest.fixture
def wired_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub everything `run` reaches the network through, and restore Pillow's global limit."""
    RecordingService.instances = []

    async def connect(settings: Settings) -> object:
        return MagicMock()

    async def close(client: object) -> None:
        return None

    async def no_loop(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(worker, "service_client", connect)
    monkeypatch.setattr(worker, "close_client", close)
    monkeypatch.setattr(worker, "work_loop", no_loop)
    monkeypatch.setattr(worker, "AnalysisService", RecordingService)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", Image.MAX_IMAGE_PIXELS)
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-secret")


async def test_the_upload_limits_go_to_the_service(wired_worker: None) -> None:
    await worker.run(Settings(MAX_UPLOAD_MB=3, MAX_IMAGE_PIXELS=1234), asyncio.Event())

    (service,) = RecordingService.instances
    assert service.kwargs["max_bytes"] == 3 * 1024 * 1024
    assert service.kwargs["max_pixels"] == 1234


async def test_the_worker_sets_pillows_pixel_limit(wired_worker: None) -> None:
    await worker.run(Settings(MAX_IMAGE_PIXELS=1234), asyncio.Event())

    assert Image.MAX_IMAGE_PIXELS == 1234
