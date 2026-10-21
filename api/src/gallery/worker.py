"""Background worker: claims jobs from the Postgres `jobs` queue and runs each through the handler
registered for its kind. ``analyze_image`` (AnalysisService) is the only kind so far.

Run with ``python -m gallery.worker``. The worker is the only component that holds the Supabase
secret key, and it has no ingress: it polls.
"""

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable, Iterator, Mapping

from PIL import Image

from gallery.adapters.analyzers import build_analyzer
from gallery.adapters.embeddings import build_embedder
from gallery.adapters.supabase_cache import SupabaseAnalysisCache
from gallery.adapters.supabase_client import close_client, service_client
from gallery.adapters.supabase_jobs import SupabaseJobQueue
from gallery.adapters.supabase_storage import SupabaseObjectStore
from gallery.adapters.verifiers import build_tag_verifier
from gallery.config import Settings, get_settings
from gallery.domain.models import Job
from gallery.observability import init_observability
from gallery.ports import JobQueue
from gallery.services.analysis import ANALYZE_IMAGE, AnalysisService

logger = logging.getLogger(__name__)

# How long a shutdown waits for running jobs. Whatever is still running after that is abandoned
# and reclaimed by the next claim once it goes stale.
DRAIN_TIMEOUT_SECONDS = 30.0
# A single failed claim is a blip. This many in a row means the worker is cut off, which is worth
# an ERROR (and so a Sentry event).
CLAIM_FAILURES_BEFORE_ERROR = 5

# Runs one claimed job to its end and records the outcome in the queue. It must not raise.
type JobHandler = Callable[[Job], Awaitable[None]]


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
    # Built first, so a misconfigured analyzer, verifier or embedder stops the worker before it
    # opens any connection.
    analyzer = build_analyzer(settings)
    verifier = build_tag_verifier(settings)
    embedder = build_embedder(settings)
    # Backstop for the AnalysisService check: a decode that gets past it fails instead of
    # allocating without bound (Pillow raises above twice this, and warns between one and two).
    Image.MAX_IMAGE_PIXELS = settings.MAX_IMAGE_PIXELS
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
            max_bytes=settings.MAX_UPLOAD_MB * 1024 * 1024,
            max_pixels=settings.MAX_IMAGE_PIXELS,
            max_attempts=settings.JOB_MAX_ATTEMPTS,
            verifier=verifier,
            min_tag_probability=settings.TAG_MIN_PROBABILITY,
            embedder=embedder,
        )
        handlers: dict[str, JobHandler] = {ANALYZE_IMAGE: service.process}
        logger.info(
            "worker started",
            extra={
                "job_kinds": sorted(handlers),
                "analyzer_id": analyzer.analyzer_id,
                "tag_verifier": settings.TAG_VERIFIER,
                "embedding_model": embedder.model_id if embedder else None,
                "concurrency": settings.WORKER_CONCURRENCY,
                "poll_min_seconds": settings.WORKER_POLL_MIN_SECONDS,
                "poll_max_seconds": settings.WORKER_POLL_MAX_SECONDS,
            },
        )
        with _stop_on_signals(stop):
            await work_loop(
                queue,
                handlers,
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
    handlers: Mapping[str, JobHandler],
    *,
    concurrency: int,
    poll_min_seconds: float,
    poll_max_seconds: float,
    stop: asyncio.Event,
    drain_timeout_seconds: float = DRAIN_TIMEOUT_SECONDS,
) -> None:
    """Claim jobs and run them, at most ``concurrency`` at a time, until ``stop`` is set.

    Only the kinds in ``handlers`` are claimed, and each job runs through the handler of its kind.
    A job of any other kind can only come from a broken queue, and it is failed for good.

    The sleep between claims starts at ``poll_min_seconds``, doubles after each one up to
    ``poll_max_seconds``, and returns to the minimum when a claim brings work. A job that
    finishes cuts the sleep short, because it freed a slot that queued work may be waiting for.
    Once ``stop`` is set, nothing more is claimed and running jobs get ``drain_timeout_seconds``
    to finish.
    """
    kinds = list(handlers)
    semaphore = asyncio.Semaphore(concurrency)
    in_flight: set[asyncio.Task[None]] = set()
    # Set whenever a job finishes, and cleared before each claim. A job that ends while the claim
    # is in flight leaves it set, so the next sleep returns at once and claims again.
    slot_freed = asyncio.Event()
    delay = poll_min_seconds
    claim_failures = 0

    def job_finished(task: asyncio.Task[None]) -> None:
        in_flight.discard(task)
        slot_freed.set()

    async def run_one(job: Job) -> None:
        async with semaphore:
            handler = handlers.get(job.kind)
            try:
                if handler is None:
                    await _fail_unknown_kind(queue, job)
                else:
                    await handler(job)
            except Exception:
                # A handler is meant to catch everything. This keeps a bug in one from ending
                # the task silently.
                logger.exception("job handler raised", extra={"job_id": job.id, "kind": job.kind})

    while not stop.is_set():
        free = concurrency - len(in_flight)
        if free <= 0:
            await _wait_for_slot(in_flight, stop)
            continue

        slot_freed.clear()
        try:
            jobs = await queue.claim(kinds, free)
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
            task.add_done_callback(job_finished)
        if jobs:
            delay = poll_min_seconds
            if len(in_flight) >= concurrency:
                # Full: the next pass waits for a slot instead of sleeping.
                continue

        await _sleep_or_stop(stop, delay, wake=slot_freed)
        delay = min(delay * 2, poll_max_seconds)

    await _drain(in_flight, drain_timeout_seconds)


async def _fail_unknown_kind(queue: JobQueue, job: Job) -> None:
    """Fail a job that no handler is registered for. It is a bug, so it is logged as an error."""
    logger.error(
        "no handler for the job's kind, failing it", extra={"job_id": job.id, "kind": job.kind}
    )
    await queue.fail(job.id, error=f"No handler for job kind {job.kind!r}", retryable=False)


async def _sleep_or_stop(
    stop: asyncio.Event, seconds: float, *, wake: asyncio.Event | None = None
) -> None:
    """Sleep for ``seconds``, or less if ``stop`` is set, or ``wake`` when one is given."""
    if wake is None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        return
    waiters = {asyncio.create_task(stop.wait()), asyncio.create_task(wake.wait())}
    try:
        await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()


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
