"""Analysis worker: claims jobs from the Postgres queue and runs each through AnalysisService.

Run with ``python -m gallery.worker``. The worker is the only component that holds the Supabase
secret key, and it has no ingress: it polls.
"""

import asyncio
import contextlib
import logging
import signal
from collections.abc import Iterator
from typing import Protocol

from gallery.adapters.analyzers import build_analyzer
from gallery.adapters.supabase_cache import SupabaseAnalysisCache
from gallery.adapters.supabase_client import close_client, service_client
from gallery.adapters.supabase_jobs import SupabaseJobQueue
from gallery.adapters.supabase_storage import SupabaseObjectStore
from gallery.adapters.verifiers import build_tag_verifier
from gallery.config import Settings, get_settings
from gallery.domain.models import AnalysisJob
from gallery.observability import init_observability
from gallery.ports import JobQueue
from gallery.services.analysis import AnalysisService

logger = logging.getLogger(__name__)

# How long a shutdown waits for running jobs. Whatever is still running after that is abandoned
# and reclaimed by the next claim once it goes stale.
DRAIN_TIMEOUT_SECONDS = 30.0
# A single failed claim is a blip. This many in a row means the worker is cut off, which is worth
# an ERROR (and so a Sentry event).
CLAIM_FAILURES_BEFORE_ERROR = 5


class JobRunner(Protocol):
    """What the loop needs from the service. ``process`` must not raise."""

    async def process(self, job: AnalysisJob) -> None: ...


def main() -> None:
    """Start the worker and run until SIGTERM or SIGINT."""
    settings = get_settings()
    init_observability(settings, "worker")
    asyncio.run(run(settings))


async def run(settings: Settings, stop: asyncio.Event | None = None) -> None:
    """Wire the adapters to the service and poll until ``stop`` is set.

    SIGTERM and SIGINT set ``stop``. Passing your own event lets a caller, such as a test, end
    the worker without a signal.
    """
    stop = stop or asyncio.Event()
    # Built first, so a misconfigured analyzer or verifier stops the worker before it opens any
    # connection.
    analyzer = build_analyzer(settings)
    verifier = build_tag_verifier(settings)
    client = await service_client(settings)
    try:
        queue = SupabaseJobQueue(
            client,
            stale_after_seconds=settings.JOB_STALE_AFTER_SECONDS,
            max_attempts=settings.JOB_MAX_ATTEMPTS,
        )
        service = AnalysisService(
            queue,
            SupabaseObjectStore(client, settings.STORAGE_BUCKET),
            analyzer,
            SupabaseAnalysisCache(client),
            max_attempts=settings.JOB_MAX_ATTEMPTS,
            verifier=verifier,
            min_tag_probability=settings.TAG_MIN_PROBABILITY,
        )
        logger.info(
            "worker started",
            extra={
                "analyzer_id": analyzer.analyzer_id,
                "tag_verifier": settings.TAG_VERIFIER,
                "concurrency": settings.WORKER_CONCURRENCY,
                "poll_min_seconds": settings.WORKER_POLL_MIN_SECONDS,
                "poll_max_seconds": settings.WORKER_POLL_MAX_SECONDS,
            },
        )
        with _stop_on_signals(stop):
            await work_loop(
                queue,
                service,
                concurrency=settings.WORKER_CONCURRENCY,
                poll_min_seconds=settings.WORKER_POLL_MIN_SECONDS,
                poll_max_seconds=settings.WORKER_POLL_MAX_SECONDS,
                stop=stop,
            )
        logger.info("worker stopped")
    finally:
        await close_client(client)


async def work_loop(
    queue: JobQueue,
    runner: JobRunner,
    *,
    concurrency: int,
    poll_min_seconds: float,
    poll_max_seconds: float,
    stop: asyncio.Event,
    drain_timeout_seconds: float = DRAIN_TIMEOUT_SECONDS,
) -> None:
    """Claim jobs and run them, at most ``concurrency`` at a time, until ``stop`` is set.

    The sleep between claims starts at ``poll_min_seconds``, doubles after each one up to
    ``poll_max_seconds``, and returns to the minimum when a claim brings work. Once ``stop`` is
    set, nothing more is claimed and running jobs get ``drain_timeout_seconds`` to finish.
    """
    semaphore = asyncio.Semaphore(concurrency)
    in_flight: set[asyncio.Task[None]] = set()
    delay = poll_min_seconds
    claim_failures = 0

    async def run_one(job: AnalysisJob) -> None:
        async with semaphore:
            try:
                await runner.process(job)
            except Exception:
                # `process` is meant to catch everything. This keeps a bug in it from ending
                # the task silently.
                logger.exception("job runner raised", extra={"image_id": job.image_id})

    while not stop.is_set():
        free = concurrency - len(in_flight)
        if free <= 0:
            await _wait_for_slot(in_flight, stop)
            continue

        try:
            jobs = await queue.claim(free)
        except Exception:
            claim_failures += 1
            # A transient failure is a warning. A long run of them is an error.
            level = (
                logging.ERROR if claim_failures == CLAIM_FAILURES_BEFORE_ERROR else logging.WARNING
            )
            logger.log(
                level,
                "could not claim jobs, backing off",
                exc_info=True,
                extra={
                    "consecutive_failures": claim_failures,
                    "retry_in_seconds": poll_max_seconds,
                },
            )
            delay = poll_max_seconds
            await _sleep_or_stop(stop, delay)
            continue
        claim_failures = 0

        for job in jobs:
            task = asyncio.create_task(run_one(job))
            in_flight.add(task)
            task.add_done_callback(in_flight.discard)
        if jobs:
            delay = poll_min_seconds
            if len(in_flight) >= concurrency:
                # Full: the next pass waits for a slot instead of sleeping.
                continue

        await _sleep_or_stop(stop, delay)
        delay = min(delay * 2, poll_max_seconds)

    await _drain(in_flight, drain_timeout_seconds)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Sleep for ``seconds``, or less if ``stop`` is set meanwhile."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def _wait_for_slot(in_flight: set[asyncio.Task[None]], stop: asyncio.Event) -> None:
    """Wait until a running job finishes or ``stop`` is set."""
    stopped = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({stopped, *in_flight}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopped.cancel()


async def _drain(in_flight: set[asyncio.Task[None]], timeout_seconds: float) -> None:
    """Give running jobs ``timeout_seconds`` to finish, then cancel the rest."""
    if not in_flight:
        return
    logger.info("draining running jobs", extra={"in_flight": len(in_flight)})
    _, abandoned = await asyncio.wait(in_flight, timeout=timeout_seconds)
    if not abandoned:
        return
    logger.warning(
        "drain timed out, abandoning running jobs; they will be reclaimed once stale",
        extra={"abandoned": len(abandoned)},
    )
    for task in abandoned:
        task.cancel()
    await asyncio.gather(*abandoned, return_exceptions=True)


@contextlib.contextmanager
def _stop_on_signals(stop: asyncio.Event) -> Iterator[None]:
    """Set ``stop`` on SIGTERM or SIGINT, for as long as the block runs."""
    loop = asyncio.get_running_loop()
    handled = (signal.SIGTERM, signal.SIGINT)

    def request_stop(received: signal.Signals) -> None:
        logger.info("shutdown requested", extra={"signal": received.name})
        stop.set()

    for received in handled:
        loop.add_signal_handler(received, request_stop, received)
    try:
        yield
    finally:
        for received in handled:
            loop.remove_signal_handler(received)


if __name__ == "__main__":
    main()
