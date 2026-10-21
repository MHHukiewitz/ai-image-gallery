"""The worker against the local stack: the jobs queue, the result cache, storage and `FakeAnalyzer`.

Start the stack with `make dev-db`, then export its values and run the tests:

    set -a; eval "$(supabase status -o env)"; set +a
    cd api && uv run pytest -m integration tests/integration/test_worker_pipeline.py

The worker claims every tenant's pending jobs, so run these while no other test expects an image
to stay 'pending'.
"""

import asyncio
import hashlib
import logging
import os
import random
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any
from uuid import UUID

import httpx
import pytest
from PIL import Image

from gallery import worker
from gallery.adapters.analyzers import FakeAnalyzer
from gallery.adapters.supabase_cache import SupabaseAnalysisCache
from gallery.adapters.supabase_client import AsyncClient, close_client, service_client, user_client
from gallery.adapters.supabase_images import SupabaseImageRepo
from gallery.adapters.supabase_jobs import SupabaseJobQueue
from gallery.adapters.supabase_storage import SupabaseObjectStore
from gallery.config import Settings
from gallery.domain.models import (
    Analysis,
    AnalysisStatus,
    AnalysisTarget,
    AnalysisUsage,
    ImageRecord,
    NewImage,
)
from gallery.services.analysis import ANALYZE_IMAGE, UNDECODABLE_IMAGE_ERROR, AnalysisService

pytestmark = pytest.mark.integration

# conftest.py clears the settings variables before every test, so read the stack's values now,
# while the tests are being collected.
_ENVIRONMENT = dict(os.environ)

FAKE_ANALYZER_ID = "fake:v1"
# The acceptance criterion: a pending upload is done within this long of the worker starting.
DEADLINE_SECONDS = 5.0


def _stack_value(*names: str) -> str | None:
    return next((_ENVIRONMENT[name] for name in names if _ENVIRONMENT.get(name)), None)


@pytest.fixture
def stack_settings() -> Settings:
    url = _stack_value("SUPABASE_URL", "API_URL")
    publishable_key = _stack_value("SUPABASE_PUBLISHABLE_KEY", "PUBLISHABLE_KEY", "ANON_KEY")
    secret_key = _stack_value("SUPABASE_SECRET_KEY", "SECRET_KEY", "SERVICE_ROLE_KEY")
    if url is None or publishable_key is None or secret_key is None:
        pytest.fail(
            "The local Supabase stack's values (including the secret key) are not in the "
            'environment. Run `make dev-db`, then `set -a; eval "$(supabase status -o env)"; '
            "set +a`."
        )
    return Settings(
        SUPABASE_URL=url,
        SUPABASE_PUBLISHABLE_KEY=publishable_key,
        SUPABASE_SECRET_KEY=secret_key,
        ANALYZER="fake",
        WORKER_POLL_MIN_SECONDS=0.1,
        WORKER_POLL_MAX_SECONDS=0.5,
    )


@dataclass(frozen=True)
class Owner:
    id: UUID
    images: SupabaseImageRepo
    objects: SupabaseObjectStore
    # Every object path the test stored, so the fixture can remove them.
    uploaded: list[str] = field(default_factory=list)


@pytest.fixture
async def secret_client(stack_settings: Settings) -> AsyncIterator[AsyncClient]:
    client = await service_client(stack_settings)
    try:
        yield client
    finally:
        await close_client(client)


@pytest.fixture
async def owner(stack_settings: Settings) -> AsyncIterator[Owner]:
    """A freshly signed-up user, whose rows and objects are removed afterwards."""
    async with httpx.AsyncClient(base_url=stack_settings.SUPABASE_URL) as http:
        signup = await http.post(
            "/auth/v1/signup",
            headers={"apikey": stack_settings.SUPABASE_PUBLISHABLE_KEY},
            json={"email": f"{uuid.uuid4().hex}@example.test", "password": "test-password-123"},
        )
    signup.raise_for_status()
    body = signup.json()
    user_id = UUID(body["user"]["id"])
    client = await user_client(stack_settings, body["access_token"])
    objects = SupabaseObjectStore(client, stack_settings.STORAGE_BUCKET)
    owner = Owner(user_id, SupabaseImageRepo(client), objects)
    try:
        yield owner
    finally:
        await objects.delete(owner.uploaded)
        await close_client(client)
        secret_key = stack_settings.SUPABASE_SECRET_KEY or ""
        async with httpx.AsyncClient(base_url=stack_settings.SUPABASE_URL) as http:
            # Deleting the user cascades to their images and image_metadata rows.
            await http.delete(
                f"/auth/v1/admin/users/{user_id}",
                headers={"apikey": secret_key, "Authorization": f"Bearer {secret_key}"},
            )


def _random_png() -> bytes:
    """A small image no earlier run has uploaded, so it is a cache miss."""
    generator = random.Random(uuid.uuid4().int)
    image = Image.new("RGB", (48, 32))
    image.putdata([tuple(generator.randrange(256) for _ in range(3)) for _ in range(48 * 32)])
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def _upload(
    owner: Owner,
    data: bytes,
    *,
    filename: str = "photo.png",
    content_sha256: str | None = None,
) -> ImageRecord:
    """Store `data` and insert the image with its pending metadata, as the API's upload does.

    `content_sha256` overrides the row's hash, as a user inserting through PostgREST can.
    """
    folder = f"{owner.id}/{uuid.uuid4()}"
    original_path = f"{folder}/original.png"
    await owner.objects.put(original_path, data, "image/png")
    owner.uploaded.append(original_path)
    return await owner.images.insert(
        NewImage(
            user_id=owner.id,
            filename=filename,
            original_path=original_path,
            thumbnail_path=f"{folder}/thumb.webp",
            mime_type="image/png",
            size_bytes=len(data),
            width=48,
            height=32,
            content_sha256=content_sha256 or hashlib.sha256(data).hexdigest(),
        )
    )


@asynccontextmanager
async def _running_worker(settings: Settings) -> AsyncIterator[asyncio.Task[None]]:
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(settings, stop))
    try:
        yield task
    finally:
        stop.set()
        # run() drains running jobs for up to 30 s, so a clean exit proves the shutdown works.
        await asyncio.wait_for(task, timeout=35)


async def _wait_for(
    owner: Owner,
    image_id: int,
    task: asyncio.Task[None],
    done: Callable[[ImageRecord], bool],
) -> ImageRecord:
    deadline = time.monotonic() + DEADLINE_SECONDS
    while True:
        if task.done():
            task.result()  # surfaces a worker that crashed
            pytest.fail("the worker stopped before the image was processed")
        record = await owner.images.get(image_id)
        assert record is not None
        if done(record):
            return record
        if time.monotonic() > deadline:
            pytest.fail(f"image {image_id} is still {record.status} after {DEADLINE_SECONDS} s")
        await asyncio.sleep(0.05)


def _is_finished(record: ImageRecord) -> bool:
    return record.status in (AnalysisStatus.DONE, AnalysisStatus.FAILED)


async def _job_row(client: AsyncClient, image_id: int) -> dict[str, Any]:
    """The image's newest job."""
    response = (
        await client.table("jobs")
        .select("id,kind,user_id,status,attempts,last_error")
        .eq("image_id", image_id)
        .order("id", desc=True)
        .limit(1)
        .single()
        .execute()
    )
    row: dict[str, Any] = response.data
    return row


async def _wait_for_job(
    client: AsyncClient, image_id: int, task: asyncio.Task[None], status: str
) -> dict[str, Any]:
    deadline = time.monotonic() + DEADLINE_SECONDS
    while True:
        if task.done():
            task.result()  # surfaces a worker that crashed
            pytest.fail("the worker stopped before the job was processed")
        row = await _job_row(client, image_id)
        if row["status"] == status:
            return row
        if time.monotonic() > deadline:
            pytest.fail(f"the job of image {image_id} is still {row['status']}")
        await asyncio.sleep(0.05)


async def _metadata_row(client: AsyncClient, image_id: int) -> dict[str, Any]:
    response = (
        await client.table("image_metadata")
        .select("ai_processing_status,attempts,analyzer_id,last_error,next_attempt_at")
        .eq("image_id", image_id)
        .single()
        .execute()
    )
    row: dict[str, Any] = response.data
    return row


# --- the pipeline -----------------------------------------------------------------------------


async def test_an_upload_goes_through_its_job_to_done(
    stack_settings: Settings, owner: Owner, secret_client: AsyncClient
) -> None:
    uploaded = await _upload(owner, _random_png())
    queued = await _job_row(secret_client, uploaded.id)
    assert (queued["kind"], queued["status"], queued["attempts"], queued["user_id"]) == (
        ANALYZE_IMAGE,
        "pending",
        0,
        str(owner.id),
    )

    async with _running_worker(stack_settings) as task:
        record = await _wait_for(owner, uploaded.id, task, _is_finished)

    assert record.status is AnalysisStatus.DONE
    # The image and its job are completed in one transaction, so the job is done by now.
    finished = await _job_row(secret_client, uploaded.id)
    assert (finished["id"], finished["status"], finished["attempts"], finished["last_error"]) == (
        queued["id"],
        "done",
        1,
        None,
    )
    row = await _metadata_row(secret_client, uploaded.id)
    assert (row["ai_processing_status"], row["attempts"], row["analyzer_id"]) == (
        "done",
        1,
        FAKE_ANALYZER_ID,
    )


async def test_a_job_the_old_worker_finished_is_closed_without_a_second_analysis(
    stack_settings: Settings,
    owner: Owner,
    secret_client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Between the jobs migration and the deploy, the worker from before still analyzes uploads."""
    caplog.set_level(logging.INFO, logger="gallery.services.analysis")
    data = _random_png()
    uploaded = await _upload(owner, data)
    # What that worker does: it finishes the image through the old RPC and never touches the job.
    await secret_client.rpc(
        "complete_analysis_job",
        {
            "p_image_id": uploaded.id,
            "p_tags": ["old", "worker", "tags", "five", "here"],
            "p_description": "Analyzed by the old worker.",
            "p_colors": ["#112233"],
            "p_analyzer_id": "old:worker:v1",
        },
    ).execute()
    assert (await _job_row(secret_client, uploaded.id))["status"] == "pending"

    async with _running_worker(stack_settings) as task:
        await _wait_for_job(secret_client, uploaded.id, task, "done")

    record = await owner.images.get(uploaded.id)
    assert record is not None
    assert (record.status, record.tags, record.description) == (
        AnalysisStatus.DONE,
        ("old", "worker", "tags", "five", "here"),
        "Analyzed by the old worker.",
    )
    assert (await _metadata_row(secret_client, uploaded.id))["analyzer_id"] == "old:worker:v1"
    lines = [r for r in caplog.records if hasattr(r, "outcome") and r.image_id == uploaded.id]
    assert [(line.outcome, line.analysis_status) for line in lines] == [("skipped", "done")]
    # The analyzer never saw the image, so nothing was cached for its bytes.
    sha = hashlib.sha256(data).hexdigest()
    assert await SupabaseAnalysisCache(secret_client).get(sha, FAKE_ANALYZER_ID) is None


async def test_concurrent_claims_never_share_a_job(
    stack_settings: Settings, owner: Owner, secret_client: AsyncClient
) -> None:
    uploads = [await _upload(owner, _random_png(), filename=f"{n}.png") for n in range(8)]
    queue = SupabaseJobQueue(secret_client, stale_after_seconds=300, max_attempts=3)

    # Six claims at once, each in its own transaction, for more slots than there are jobs.
    claims = await asyncio.gather(*(queue.claim([ANALYZE_IMAGE], 3) for _ in range(6)))

    claimed = [job for jobs in claims for job in jobs]
    assert len({job.id for job in claimed}) == len(claimed)
    assert len(claimed) >= len(uploads)
    # Run what was claimed, so no job is left 'processing'.
    service = AnalysisService(
        queue,
        SupabaseObjectStore(secret_client, stack_settings.STORAGE_BUCKET),
        FakeAnalyzer(),
        SupabaseAnalysisCache(secret_client),
        max_bytes=stack_settings.MAX_UPLOAD_MB * 1024 * 1024,
        max_pixels=stack_settings.MAX_IMAGE_PIXELS,
    )
    await asyncio.gather(*(service.process(job) for job in claimed))
    for uploaded in uploads:
        record = await owner.images.get(uploaded.id)
        assert record is not None
        assert record.status is AnalysisStatus.DONE


async def test_a_pending_upload_reaches_done_within_five_seconds(
    stack_settings: Settings, owner: Owner, secret_client: AsyncClient
) -> None:
    data = _random_png()
    uploaded = await _upload(owner, data)
    assert uploaded.status is AnalysisStatus.PENDING

    started = time.monotonic()
    async with _running_worker(stack_settings) as task:
        record = await _wait_for(owner, uploaded.id, task, _is_finished)
    elapsed = time.monotonic() - started

    assert record.status is AnalysisStatus.DONE
    assert elapsed < DEADLINE_SECONDS
    assert len(record.tags) == 5
    assert 1 <= len(record.colors) <= 3
    assert all(len(color) == 7 and color.startswith("#") for color in record.colors)
    assert record.description
    assert record.error is None
    row = await _metadata_row(secret_client, uploaded.id)
    assert row["analyzer_id"] == FAKE_ANALYZER_ID
    assert row["attempts"] == 1
    cached = await SupabaseAnalysisCache(secret_client).get(
        hashlib.sha256(data).hexdigest(), FAKE_ANALYZER_ID
    )
    assert cached == Analysis(tags=record.tags, description=record.description)


async def test_the_same_bytes_uploaded_again_are_served_from_the_cache(
    stack_settings: Settings,
    owner: Owner,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="gallery.services.analysis")
    data = _random_png()
    first = await _upload(owner, data, filename="first.png")
    async with _running_worker(stack_settings) as task:
        first_done = await _wait_for(
            owner, first.id, task, lambda r: r.status is AnalysisStatus.DONE
        )
        second = await _upload(owner, data, filename="second.png")
        second_done = await _wait_for(
            owner, second.id, task, lambda r: r.status is AnalysisStatus.DONE
        )

    lines = {record.image_id: record for record in caplog.records if hasattr(record, "outcome")}
    assert lines[first.id].cache_hit is False
    assert lines[second.id].cache_hit is True
    assert (second_done.tags, second_done.description) == (first_done.tags, first_done.description)


async def test_a_forged_content_hash_neither_reads_nor_writes_another_files_cache_entry(
    stack_settings: Settings, owner: Owner, secret_client: AsyncClient
) -> None:
    cache = SupabaseAnalysisCache(secret_client)
    # Someone else's file, already analyzed: its entry holds a recognizable description.
    victim_sha = hashlib.sha256(b"forged_hash_test victim " + uuid.uuid4().bytes).hexdigest()
    victims_entry = Analysis(
        tags=("victim", "tags", "from", "the", "cache"), description="The victim's picture."
    )
    await cache.put(victim_sha, FAKE_ANALYZER_ID, victims_entry)
    # A popular file nobody has analyzed yet: the poisoning attack targets its future entry.
    unanalyzed_sha = hashlib.sha256(b"forged_hash_test popular " + uuid.uuid4().bytes).hexdigest()
    data = _random_png()
    own_sha = hashlib.sha256(data).hexdigest()
    reads = await _upload(owner, data, filename="reads.png", content_sha256=victim_sha)
    writes = await _upload(
        owner, _random_png(), filename="writes.png", content_sha256=unanalyzed_sha
    )

    async with _running_worker(stack_settings) as task:
        read_done = await _wait_for(owner, reads.id, task, _is_finished)
        write_done = await _wait_for(owner, writes.id, task, _is_finished)

    assert (read_done.status, write_done.status) == (AnalysisStatus.DONE, AnalysisStatus.DONE)
    # The forged hash did not return the victim's entry: the image was analyzed on its own bytes.
    assert read_done.description != victims_entry.description
    assert await cache.get(victim_sha, FAKE_ANALYZER_ID) == victims_entry
    # Nothing was written under the forged hash; the result went under the real digest.
    assert await cache.get(unanalyzed_sha, FAKE_ANALYZER_ID) is None
    assert await cache.get(own_sha, FAKE_ANALYZER_ID) == Analysis(
        tags=read_done.tags, description=read_done.description
    )


async def test_a_corrupt_upload_fails_at_once_and_is_not_retried(
    stack_settings: Settings, owner: Owner, secret_client: AsyncClient
) -> None:
    uploaded = await _upload(owner, b"this is not an image")

    async with _running_worker(stack_settings) as task:
        record = await _wait_for(
            owner, uploaded.id, task, lambda r: r.status is AnalysisStatus.FAILED
        )

    assert record.error == UNDECODABLE_IMAGE_ERROR
    assert record.colors == ()
    assert record.tags == ()
    row = await _metadata_row(secret_client, uploaded.id)
    assert row["attempts"] == 1
    assert row["next_attempt_at"] is None


# --- the adapters, without a worker -----------------------------------------------------------


async def test_queue_analysis_target_describes_the_image_of_a_job(
    stack_settings: Settings, owner: Owner, secret_client: AsyncClient
) -> None:
    uploaded = await _upload(owner, _random_png())
    job = await _job_row(secret_client, uploaded.id)
    queue = SupabaseJobQueue(secret_client, stale_after_seconds=300, max_attempts=3)

    assert await queue.analysis_target(job["id"]) == AnalysisTarget(
        image_id=uploaded.id,
        original_path=uploaded.original_path,
        content_sha256=uploaded.content_sha256,
        status=AnalysisStatus.PENDING,
        error=None,
    )


async def test_queue_complete_analysis_stores_the_analysis_and_completes_the_job(
    stack_settings: Settings, owner: Owner, secret_client: AsyncClient
) -> None:
    uploaded = await _upload(owner, _random_png())
    job = await _job_row(secret_client, uploaded.id)
    queue = SupabaseJobQueue(secret_client, stale_after_seconds=300, max_attempts=3)

    await queue.complete_analysis(
        job["id"],
        tags=("a", "b", "c", "d", "e"),
        description="A test.",
        colors=("#112233", "#445566"),
        analyzer_id="test:model:v1",
    )

    record = await owner.images.get(uploaded.id)
    assert record is not None
    assert record.status is AnalysisStatus.DONE
    assert record.tags == ("a", "b", "c", "d", "e")
    assert record.description == "A test."
    assert record.colors == ("#112233", "#445566")
    assert (await _metadata_row(secret_client, uploaded.id))["analyzer_id"] == "test:model:v1"
    assert (await _job_row(secret_client, uploaded.id))["status"] == "done"


async def test_queue_fail_requeues_a_retryable_failure_and_ends_a_final_one(
    stack_settings: Settings, owner: Owner, secret_client: AsyncClient
) -> None:
    uploaded = await _upload(owner, _random_png())
    job = await _job_row(secret_client, uploaded.id)
    queue = SupabaseJobQueue(secret_client, stale_after_seconds=300, max_attempts=3)

    await queue.fail_analysis(job["id"], error="Provider busy", colors=["#112233"], retryable=True)

    retrying = await owner.images.get(uploaded.id)
    assert retrying is not None
    assert retrying.status is AnalysisStatus.PENDING
    assert retrying.error == "Provider busy"
    assert retrying.colors == ("#112233",)
    # It waits before it can be claimed again.
    assert (await _metadata_row(secret_client, uploaded.id))["next_attempt_at"] is not None
    assert (await _job_row(secret_client, uploaded.id))["status"] == "pending"

    await queue.fail_analysis(job["id"], error="Key rejected", colors=["#112233"], retryable=False)

    failed = await owner.images.get(uploaded.id)
    assert failed is not None
    assert failed.status is AnalysisStatus.FAILED
    assert failed.error == "Key rejected"
    assert (await _job_row(secret_client, uploaded.id))["status"] == "failed"


async def test_queue_calls_for_a_deleted_image_do_not_raise(
    stack_settings: Settings, owner: Owner, secret_client: AsyncClient
) -> None:
    uploaded = await _upload(owner, _random_png())
    job_id = (await _job_row(secret_client, uploaded.id))["id"]
    await owner.images.delete(uploaded.id)
    queue = SupabaseJobQueue(secret_client, stale_after_seconds=300, max_attempts=3)

    assert await queue.analysis_target(job_id) is None
    await queue.complete_analysis(
        job_id, tags=["a"], description="Gone.", colors=["#000000"], analyzer_id="x:y:z"
    )
    await queue.fail_analysis(job_id, error="Gone", colors=[], retryable=True)
    await queue.complete(job_id)
    await queue.fail(job_id, error="Gone", retryable=False)


async def test_cache_round_trip(secret_client: AsyncClient) -> None:
    cache = SupabaseAnalysisCache(secret_client)
    # A fixed key: ai_cache rows can't be deleted by the worker's role, so reuse the same row.
    sha = hashlib.sha256(b"test_cache_round_trip").hexdigest()
    analyzer_id = "test:cache:v1"

    first = Analysis(tags=("one", "two", "three", "four", "five"), description="First.")
    second = Analysis(
        tags=("six", "seven", "eight", "nine", "ten"),
        description="Second.",
        usage=AnalysisUsage(latency_ms=10, input_tokens=1, output_tokens=2, cost_usd=0.1),
    )

    await cache.put(sha, analyzer_id, first)
    assert await cache.get(sha, analyzer_id) == first

    # A second put replaces the row, and the usage numbers are not stored.
    await cache.put(sha, analyzer_id, second)
    assert await cache.get(sha, analyzer_id) == Analysis(
        tags=second.tags, description=second.description
    )
    assert await cache.get(sha, "another:analyzer:v1") is None
    assert await cache.get(hashlib.sha256(b"never stored").hexdigest(), analyzer_id) is None


async def test_cache_treats_a_malformed_row_as_a_miss(secret_client: AsyncClient) -> None:
    cache = SupabaseAnalysisCache(secret_client)
    sha = hashlib.sha256(b"test_cache_malformed_row").hexdigest()
    analyzer_id = "test:cache:malformed"
    await (
        secret_client.table("ai_cache")
        .upsert(
            {"content_sha256": sha, "analyzer_id": analyzer_id, "result": {"unexpected": True}},
            on_conflict="content_sha256,analyzer_id",
        )
        .execute()
    )

    assert await cache.get(sha, analyzer_id) is None
