"""Embeddings against the local stack: the RPC, the service, "find similar" and the backfill.

Start the stack with `make dev-db`, then export its values and run the tests:

    set -a; eval "$(supabase status -o env)"; set +a
    cd api && uv run pytest -m integration tests/integration/test_embeddings_pipeline.py

They use `FakeEmbedder`, so no model is needed. The secret key is required: it stores analyses and
embeddings the way the worker does, and it deletes the test users at the end, which removes their
images too. The backlog is global, so the backfill test only looks at its own images.
"""

import json
import os
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import pytest
from postgrest import APIError

from gallery.adapters.analyzers import FakeAnalyzer
from gallery.adapters.embeddings import FakeEmbedder
from gallery.adapters.supabase_cache import SupabaseAnalysisCache
from gallery.adapters.supabase_client import (
    AsyncClient,
    close_client,
    service_client,
    user_client,
)
from gallery.adapters.supabase_images import SupabaseImageRepo
from gallery.adapters.supabase_jobs import SupabaseJobQueue
from gallery.api import deps
from gallery.backfill_embeddings import BacklogRow, SupabaseBacklog, backfill
from gallery.config import Settings
from gallery.domain.embedding_text import embedding_text
from gallery.domain.models import AnalysisJob, NewImage
from gallery.services.analysis import AnalysisService

pytestmark = pytest.mark.integration

# conftest.py clears the settings variables before every test, so read the stack's values now,
# while the tests are being collected.
_ENVIRONMENT = dict(os.environ)

_ANALYZER_ID = "test:fake:1"
_EMBEDDER = FakeEmbedder()


def _stack_value(*names: str) -> str | None:
    return next((_ENVIRONMENT[name] for name in names if _ENVIRONMENT.get(name)), None)


@pytest.fixture
def stack_settings() -> Settings:
    url = _stack_value("SUPABASE_URL", "API_URL")
    publishable_key = _stack_value("SUPABASE_PUBLISHABLE_KEY", "PUBLISHABLE_KEY", "ANON_KEY")
    secret_key = _stack_value("SUPABASE_SECRET_KEY", "SECRET_KEY", "SERVICE_ROLE_KEY")
    if url is None or publishable_key is None or secret_key is None:
        pytest.fail(
            "The local Supabase stack's values are not in the environment. Run `make dev-db`, "
            'then `set -a; eval "$(supabase status -o env)"; set +a`.'
        )
    return Settings(
        SUPABASE_URL=url,
        SUPABASE_PUBLISHABLE_KEY=publishable_key,
        SUPABASE_SECRET_KEY=secret_key,
        SUPABASE_JWT_SECRET=_stack_value("SUPABASE_JWT_SECRET", "JWT_SECRET"),
    )


@dataclass(frozen=True)
class Tenant:
    id: UUID
    access_token: str
    client: AsyncClient
    images: SupabaseImageRepo


@pytest.fixture
async def alice(stack_settings: Settings) -> AsyncIterator[Tenant]:
    async with httpx.AsyncClient(base_url=stack_settings.SUPABASE_URL) as http:
        signup = await http.post(
            "/auth/v1/signup",
            headers={"apikey": stack_settings.SUPABASE_PUBLISHABLE_KEY},
            json={"email": f"{uuid.uuid4().hex}@example.test", "password": "test-password-123"},
        )
    signup.raise_for_status()
    body = signup.json()
    user_id, access_token = UUID(body["user"]["id"]), body["access_token"]
    client = await user_client(stack_settings, access_token)
    try:
        yield Tenant(user_id, access_token, client, SupabaseImageRepo(client))
    finally:
        await close_client(client)
        secret_key = stack_settings.SUPABASE_SECRET_KEY or ""
        async with httpx.AsyncClient(base_url=stack_settings.SUPABASE_URL) as http:
            response = await http.delete(
                f"/auth/v1/admin/users/{user_id}",
                headers={"apikey": secret_key, "Authorization": f"Bearer {secret_key}"},
            )
        # A user left behind would keep their images in the shared database, so fail loudly.
        response.raise_for_status()


@pytest.fixture
async def worker(stack_settings: Settings) -> AsyncIterator[AsyncClient]:
    client = await service_client(stack_settings)
    try:
        yield client
    finally:
        await close_client(client)


@pytest.fixture
def queue(worker: AsyncClient) -> SupabaseJobQueue:
    return SupabaseJobQueue(worker, stale_after_seconds=300, max_attempts=3)


@pytest.fixture
async def api(stack_settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    # gallery.main builds the app when it is imported, and that reads the settings. Import it
    # here, after conftest's autouse fixture has set them, instead of at module level.
    from gallery.main import create_app

    app = create_app()
    app.dependency_overrides[deps.get_settings] = lambda: stack_settings
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def _add_image(owner: Tenant, *, filename: str = "photo.png") -> int:
    """Insert an image, as the upload does. Its analysis is still pending."""
    folder = f"{owner.id}/{uuid.uuid4()}"
    record = await owner.images.insert(
        NewImage(
            user_id=owner.id,
            filename=filename,
            original_path=f"{folder}/original.png",
            thumbnail_path=f"{folder}/thumb.webp",
            mime_type="image/png",
            size_bytes=1234,
            width=640,
            height=480,
            content_sha256=uuid.uuid4().hex * 2,
        )
    )
    return record.id


async def _add_embedded_image(
    owner: Tenant,
    worker: AsyncClient,
    queue: SupabaseJobQueue,
    *,
    tags: Sequence[str],
    description: str,
    embedded: bool = True,
) -> int:
    """Insert an image, store its analysis and, unless told not to, the FakeEmbedder's vector."""
    image_id = await _add_image(owner)
    await worker.rpc(
        "complete_analysis_job",
        {
            "p_image_id": image_id,
            "p_tags": list(tags),
            "p_description": description,
            "p_colors": [],
            "p_analyzer_id": _ANALYZER_ID,
        },
    ).execute()
    if embedded:
        (vector,) = await _EMBEDDER.embed([embedding_text(tags, description)])
        await queue.store_embedding(image_id, vector, _EMBEDDER.model_id)
    return image_id


async def _stored_embedding(
    worker: AsyncClient, image_id: int
) -> tuple[list[float] | None, str | None]:
    response = (
        await worker.table("image_metadata")
        .select("embedding, embedding_model")
        .eq("image_id", image_id)
        .single()
        .execute()
    )
    row: dict[str, Any] = response.data
    # PostgREST returns a vector as the text "[0.1,0.2,...]".
    vector = json.loads(row["embedding"]) if row["embedding"] else None
    return vector, row["embedding_model"]


def _as(tenant: Tenant) -> dict[str, str]:
    return {"Authorization": f"Bearer {tenant.access_token}"}


# --- the RPC ----------------------------------------------------------------------------------


async def test_store_embedding_writes_the_vector_and_model_id(
    alice: Tenant, worker: AsyncClient, queue: SupabaseJobQueue
) -> None:
    image_id = await _add_image(alice)
    (vector,) = await _EMBEDDER.embed(["a puppy on the grass"])

    await queue.store_embedding(image_id, vector, _EMBEDDER.model_id)

    stored, model = await _stored_embedding(worker, image_id)
    assert model == _EMBEDDER.model_id
    assert stored is not None
    assert len(stored) == 384
    # pgvector keeps 32-bit floats.
    assert stored == pytest.approx(vector, abs=1e-6)


async def test_store_embedding_for_a_deleted_image_is_not_an_error(
    queue: SupabaseJobQueue,
) -> None:
    (vector,) = await _EMBEDDER.embed(["a puppy on the grass"])

    await queue.store_embedding(-1, vector, _EMBEDDER.model_id)


async def test_store_embedding_rejects_a_vector_of_the_wrong_size(
    alice: Tenant, queue: SupabaseJobQueue
) -> None:
    image_id = await _add_image(alice)

    with pytest.raises(APIError, match="384 dimensions"):
        await queue.store_embedding(image_id, [0.1, 0.2, 0.3], _EMBEDDER.model_id)


async def test_a_signed_in_user_cannot_store_an_embedding(alice: Tenant) -> None:
    image_id = await _add_image(alice)
    (vector,) = await _EMBEDDER.embed(["a puppy on the grass"])

    with pytest.raises(APIError) as error:
        await alice.client.rpc(
            "store_image_embedding",
            {"p_image_id": image_id, "p_embedding": vector, "p_model": "forged"},
        ).execute()

    assert error.value.code == "42501"


# --- the service ------------------------------------------------------------------------------


class _PngStore:
    """Hands out one tiny PNG for every path, so the service needs no storage bucket."""

    def __init__(self) -> None:
        from io import BytesIO

        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", (16, 16), (20, 160, 40)).save(buffer, format="PNG")
        self._png = buffer.getvalue()

    async def put(self, path: str, data: bytes, content_type: str) -> None:
        raise NotImplementedError

    async def get(self, path: str) -> bytes:
        return self._png

    async def delete(self, paths: Sequence[str]) -> None:
        raise NotImplementedError

    async def signed_urls(self, paths: Sequence[str], ttl_seconds: int) -> dict[str, str]:
        raise NotImplementedError


async def test_the_service_completes_a_job_and_stores_its_embedding(
    alice: Tenant, worker: AsyncClient, queue: SupabaseJobQueue
) -> None:
    image_id = await _add_image(alice)
    service = AnalysisService(
        queue,
        _PngStore(),
        FakeAnalyzer(),
        SupabaseAnalysisCache(worker),
        embedder=_EMBEDDER,
    )
    job = AnalysisJob(
        image_id=image_id,
        user_id=alice.id,
        original_path=f"{alice.id}/x/original.png",
        content_sha256=uuid.uuid4().hex * 2,
        attempts=1,
    )

    await service.process(job)

    record = await alice.images.get(image_id)
    assert record is not None
    assert record.status.value == "done"
    stored, model = await _stored_embedding(worker, image_id)
    (expected,) = await _EMBEDDER.embed([embedding_text(record.tags, record.description or "")])
    assert model == _EMBEDDER.model_id
    assert stored == pytest.approx(expected, abs=1e-6)


async def test_a_reanalysis_with_new_tags_drops_the_old_embedding(
    alice: Tenant, worker: AsyncClient, queue: SupabaseJobQueue
) -> None:
    image_id = await _add_embedded_image(
        alice, worker, queue, tags=["puppy", "grass"], description="A puppy on grass."
    )

    await worker.rpc(
        "complete_analysis_job",
        {
            "p_image_id": image_id,
            "p_tags": ["kitten", "sofa"],
            "p_description": "A kitten on a sofa.",
            "p_colors": [],
            "p_analyzer_id": _ANALYZER_ID,
        },
    ).execute()

    assert await _stored_embedding(worker, image_id) == (None, None)


# --- find similar -----------------------------------------------------------------------------


async def test_similar_images_scores_on_the_embeddings_and_the_floor(
    api: httpx.AsyncClient, alice: Tenant, worker: AsyncClient, queue: SupabaseJobQueue
) -> None:
    target = await _add_embedded_image(
        alice,
        worker,
        queue,
        tags=["puppy", "grass", "park", "dog", "playing"],
        description="A puppy playing on the grass in a park.",
    )
    near = await _add_embedded_image(
        alice,
        worker,
        queue,
        tags=["puppy", "grass", "park", "dog", "sleeping"],
        description="A puppy sleeping on the grass in a park.",
    )
    far = await _add_embedded_image(
        alice,
        worker,
        queue,
        tags=["ocean", "boat", "waves", "sailing", "blue"],
        description="A sailboat on blue ocean waves.",
    )
    floor = float((await worker.rpc("search_embedding_floor").execute()).data)

    response = await api.get("/api/v1/images", params={"similar_to": target}, headers=_as(alice))

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["id"] for item in items] == [near]
    assert far not in [item["id"] for item in items]
    texts = [
        embedding_text(
            ["puppy", "grass", "park", "dog", "playing"], "A puppy playing on the grass in a park."
        ),
        embedding_text(
            ["puppy", "grass", "park", "dog", "sleeping"],
            "A puppy sleeping on the grass in a park.",
        ),
    ]
    first, second = await _EMBEDDER.embed(texts)
    similarity = sum(a * b for a, b in zip(first, second, strict=True))
    expected = 0.7 * max(0.0, (similarity - floor) / (1 - floor))
    assert items[0]["score"] == pytest.approx(expected, abs=1e-4)


async def test_similar_images_falls_back_to_tag_words_without_an_embedding(
    api: httpx.AsyncClient, alice: Tenant, worker: AsyncClient, queue: SupabaseJobQueue
) -> None:
    target = await _add_embedded_image(
        alice,
        worker,
        queue,
        tags=["house"],
        description="A house.",
        embedded=False,
    )
    red_house = await _add_embedded_image(
        alice, worker, queue, tags=["red house"], description="A red house.", embedded=False
    )
    embedded_house = await _add_embedded_image(
        alice, worker, queue, tags=["house"], description="A house.", embedded=True
    )

    response = await api.get("/api/v1/images", params={"similar_to": target}, headers=_as(alice))

    assert response.status_code == 200
    scores = {item["id"]: item["score"] for item in response.json()["items"]}
    # The target has no embedding, so every candidate is scored on tag words, embedded or not.
    assert scores[red_house] == pytest.approx(0.7 / 2**0.5)
    assert scores[embedded_house] == pytest.approx(0.7)


# --- the backfill -----------------------------------------------------------------------------


class _OwnRows:
    """The Supabase backlog narrowed to some image ids, because the real one is global."""

    def __init__(self, inner: SupabaseBacklog, image_ids: set[int]) -> None:
        self._inner = inner
        self._image_ids = image_ids

    async def page(self, after_image_id: int, limit: int) -> list[BacklogRow]:
        cursor = after_image_id
        while rows := await self._inner.page(cursor, 100):
            mine = [row for row in rows if row.image_id in self._image_ids]
            if mine:
                return mine[:limit]
            cursor = rows[-1].image_id
        return []


async def test_the_backlog_lists_done_rows_without_an_embedding_and_the_backfill_fills_them(
    alice: Tenant, worker: AsyncClient, queue: SupabaseJobQueue
) -> None:
    missing_a = await _add_embedded_image(
        alice, worker, queue, tags=["red", "house"], description="A red house.", embedded=False
    )
    missing_b = await _add_embedded_image(
        alice, worker, queue, tags=["dog", "park"], description="A dog in a park.", embedded=False
    )
    already = await _add_embedded_image(
        alice, worker, queue, tags=["sea", "boat"], description="A boat at sea."
    )
    pending = await _add_image(alice)  # never analyzed, so not 'done'
    mine = {missing_a, missing_b, already, pending}
    backlog = SupabaseBacklog(worker)

    listed = {row.image_id: row for row in await backlog.page(0, 1000) if row.image_id in mine}

    assert set(listed) == {missing_a, missing_b}
    assert listed[missing_a].tags == ("red", "house")
    assert listed[missing_a].description == "A red house."

    counts = await backfill(_OwnRows(backlog, mine), _EMBEDDER, queue)

    assert (counts.embedded, counts.failed) == (2, 0)
    for image_id, tags, description in [
        (missing_a, ["red", "house"], "A red house."),
        (missing_b, ["dog", "park"], "A dog in a park."),
    ]:
        stored, model = await _stored_embedding(worker, image_id)
        (expected,) = await _EMBEDDER.embed([embedding_text(tags, description)])
        assert model == _EMBEDDER.model_id
        assert stored == pytest.approx(expected, abs=1e-6)
    assert (await _stored_embedding(worker, pending)) == (None, None)

    second = await backfill(_OwnRows(backlog, mine), _EMBEDDER, queue)

    assert second.scanned == 0
