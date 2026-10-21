"""Semantic text search through the API against the local stack, with real users signed up.

Start the stack with `make dev-db`, then export its values and run the tests:

    set -a; eval "$(supabase status -o env)"; set +a
    cd api && uv run pytest -m integration tests/integration/test_semantic_search.py

Most tests use `FakeEmbedder`, so no model is needed. The ones that check what the real model
finds ("puppy" returns dogs, "seaside" returns beaches) skip unless BAAI/bge-small-en-v1.5 is
already in `EMBEDDING_CACHE_DIR`, as in test_embedding_model.py, and they go through the API's lazy
loading. The images are synthetic. The secret key is required: it stores each image's analysis and
embedding the way the worker does, and it deletes the test users at the end, which removes their
images too.
"""

import os
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from gallery.adapters.embeddings import FakeEmbedder, FastEmbedEmbedder
from gallery.adapters.supabase_client import (
    AsyncClient,
    close_client,
    service_client,
    user_client,
)
from gallery.adapters.supabase_images import SupabaseImageRepo
from gallery.adapters.supabase_jobs import SupabaseJobQueue
from gallery.api import deps
from gallery.config import Settings
from gallery.domain.embedding_text import embedding_text
from gallery.domain.models import NewImage
from gallery.ports import TextEmbedder

pytestmark = pytest.mark.integration

MODEL = "BAAI/bge-small-en-v1.5"

# conftest.py clears the settings variables before every test, so read the stack's values and the
# model cache now, while the tests are being collected.
_ENVIRONMENT = dict(os.environ)
_CACHE_DIR = _ENVIRONMENT.get("EMBEDDING_CACHE_DIR")

_ANALYZER_ID = "test:fake:1"


def _model_is_cached() -> bool:
    return bool(_CACHE_DIR) and any(Path(_CACHE_DIR or "").glob("models--*bge-small-en-v1.5*"))


needs_model = pytest.mark.skipif(
    not _model_is_cached(),
    reason=f"{MODEL} is not in EMBEDDING_CACHE_DIR (see test_embedding_model.py)",
)


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
    images: SupabaseImageRepo


async def _tenant(settings: Settings) -> AsyncIterator[Tenant]:
    async with httpx.AsyncClient(base_url=settings.SUPABASE_URL) as http:
        signup = await http.post(
            "/auth/v1/signup",
            headers={"apikey": settings.SUPABASE_PUBLISHABLE_KEY},
            json={"email": f"{uuid.uuid4().hex}@example.test", "password": "test-password-123"},
        )
    signup.raise_for_status()
    body = signup.json()
    user_id, access_token = UUID(body["user"]["id"]), body["access_token"]
    client = await user_client(settings, access_token)
    try:
        yield Tenant(user_id, access_token, SupabaseImageRepo(client))
    finally:
        await close_client(client)
        secret_key = settings.SUPABASE_SECRET_KEY or ""
        async with httpx.AsyncClient(base_url=settings.SUPABASE_URL) as http:
            response = await http.delete(
                f"/auth/v1/admin/users/{user_id}",
                headers={"apikey": secret_key, "Authorization": f"Bearer {secret_key}"},
            )
        # A user left behind would keep their images in the shared database, so fail loudly.
        response.raise_for_status()


@pytest.fixture
async def alice(stack_settings: Settings) -> AsyncIterator[Tenant]:
    async for tenant in _tenant(stack_settings):
        yield tenant


@pytest.fixture
async def bob(stack_settings: Settings) -> AsyncIterator[Tenant]:
    async for tenant in _tenant(stack_settings):
        yield tenant


@pytest.fixture
async def worker(stack_settings: Settings) -> AsyncIterator[AsyncClient]:
    client = await service_client(stack_settings)
    try:
        yield client
    finally:
        await close_client(client)


@asynccontextmanager
async def _api(
    settings: Settings, embedder: TextEmbedder | None | str
) -> AsyncIterator[httpx.AsyncClient]:
    """The app over `settings`. `embedder` replaces the query embedder, unless it is "lazy"."""
    # gallery.main builds the app when it is imported, and that reads the settings. Import it
    # here, after conftest's autouse fixture has set them, instead of at module level.
    from gallery.main import create_app

    app = create_app()
    app.dependency_overrides[deps.get_settings] = lambda: settings
    if embedder != "lazy":
        app.dependency_overrides[deps.get_query_embedder] = lambda: embedder
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def _add_image(
    owner: Tenant,
    worker: AsyncClient,
    embedder: TextEmbedder,
    *,
    filename: str,
    tags: Sequence[str],
    description: str,
    embedded_text: str | None = None,
) -> int:
    """Insert an image as the upload does, then store its analysis and embedding as the worker does.

    The vector is made from `embedded_text`, or from the image's own text when that is None.
    """
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
            stored_sha256=uuid.uuid4().hex * 2,
        )
    )
    await worker.rpc(
        "complete_analysis_job",
        {
            "p_image_id": record.id,
            "p_tags": list(tags),
            "p_description": description,
            "p_colors": [],
            "p_analyzer_id": _ANALYZER_ID,
        },
    ).execute()
    text = embedded_text if embedded_text is not None else embedding_text(tags, description)
    (vector,) = await embedder.embed([text])
    queue = SupabaseJobQueue(worker, stale_after_seconds=300, max_attempts=3)
    await queue.store_embedding(record.id, vector, embedder.model_id)
    return record.id


def _as(tenant: Tenant) -> dict[str, str]:
    return {"Authorization": f"Bearer {tenant.access_token}"}


async def _search(
    api: httpx.AsyncClient, tenant: Tenant, q: str, **params: Any
) -> list[dict[str, Any]]:
    response = await api.get("/api/v1/images", params={"q": q, **params}, headers=_as(tenant))
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


# --- the mechanics, with a fake embedder: no model needed ----------------------------------


@dataclass(frozen=True)
class Seeded:
    """`tagged` has the tag "puppy". `by_meaning` has other tags, but the embedding of "puppy"
    itself, which is as close in meaning as a vector can be. `unrelated` is far from both."""

    tagged: int
    by_meaning: int
    unrelated: int


@pytest.fixture
async def seeded(alice: Tenant, worker: AsyncClient) -> Seeded:
    fake = FakeEmbedder()
    tagged = await _add_image(
        alice, worker, fake, filename="tagged.png", tags=["puppy", "grass"], description="A puppy."
    )
    by_meaning = await _add_image(
        alice,
        worker,
        fake,
        filename="by-meaning.png",
        tags=["dog", "sofa"],
        description="A dog asleep.",
        embedded_text=embedding_text(["puppy"], ""),
    )
    unrelated = await _add_image(
        alice, worker, fake, filename="unrelated.png", tags=["tractor"], description="A farm."
    )
    return Seeded(tagged, by_meaning, unrelated)


async def test_a_text_search_finds_an_image_by_meaning_and_ranks_the_exact_tag_first(
    stack_settings: Settings, alice: Tenant, seeded: Seeded
) -> None:
    async with _api(stack_settings, FakeEmbedder()) as api:
        items = await _search(api, alice, "puppy")

    assert [item["id"] for item in items] == [seeded.tagged, seeded.by_meaning]
    assert items[0]["score"] > items[1]["score"] > 0
    assert seeded.unrelated not in [item["id"] for item in items]


async def test_without_an_embedder_a_text_search_finds_words_only(
    stack_settings: Settings, alice: Tenant, seeded: Seeded
) -> None:
    async with _api(stack_settings, None) as api:
        items = await _search(api, alice, "puppy")

    assert [item["id"] for item in items] == [seeded.tagged]


async def test_a_failing_embedder_falls_back_to_words_only(
    stack_settings: Settings, alice: Tenant, seeded: Seeded
) -> None:
    class BrokenEmbedder(FakeEmbedder):
        async def embed(self, texts: Sequence[str]) -> list[list[float]]:
            raise RuntimeError("the model is gone")

    async with _api(stack_settings, BrokenEmbedder()) as api:
        items = await _search(api, alice, "puppy")

    assert [item["id"] for item in items] == [seeded.tagged]


async def test_a_semantic_search_never_returns_another_users_images(
    stack_settings: Settings, bob: Tenant, seeded: Seeded
) -> None:
    async with _api(stack_settings, FakeEmbedder()) as api:
        items = await _search(api, bob, "puppy")

    assert items == []


async def test_pages_of_a_semantic_search_add_up_to_the_whole_result(
    stack_settings: Settings, alice: Tenant, seeded: Seeded
) -> None:
    async with _api(stack_settings, FakeEmbedder()) as api:
        first = await api.get(
            "/api/v1/images", params={"q": "puppy", "limit": 1}, headers=_as(alice)
        )
        second = await api.get(
            "/api/v1/images",
            params={"q": "puppy", "limit": 1, "offset": first.json()["next_offset"]},
            headers=_as(alice),
        )

    assert [item["id"] for item in first.json()["items"]] == [seeded.tagged]
    assert first.json()["next_offset"] == 1
    assert [item["id"] for item in second.json()["items"]] == [seeded.by_meaning]
    assert second.json()["next_offset"] is None


# --- what the real model finds ----------------------------------------------------------------
# Synthetic photos in the style the analyzer writes. The queries are the ones the calibration in
# docs/architecture.md section 8 used.

PHOTOS: dict[str, tuple[str, list[str]]] = {
    "golden-puppy": (
        "A golden retriever puppy playing on green grass in a sunny park.",
        ["dog", "puppy", "grass", "park", "golden retriever", "playing", "green", "outdoor"],
    ),
    "labrador": (
        "A black labrador running along a muddy trail with a stick in its mouth.",
        ["dog", "labrador", "running", "stick", "trail", "outdoor", "black", "mud"],
    ),
    "beagles": (
        "Two beagles sitting side by side on a wooden porch and looking at the camera.",
        ["beagle", "dogs", "porch", "sitting", "pets", "wood", "camera"],
    ),
    "husky": (
        "A husky with blue eyes lying in the snow on a cold winter day.",
        ["husky", "dog", "snow", "winter", "blue eyes", "fur", "cold"],
    ),
    "beach-sunset": (
        "Waves rolling onto a sandy beach at sunset under an orange sky.",
        ["beach", "sunset", "sea", "waves", "sand", "orange sky", "horizon", "coast"],
    ),
    "palm-beach": (
        "Palm trees leaning over a tropical beach with turquoise water.",
        ["palm trees", "beach", "tropical", "turquoise", "ocean", "vacation", "sand"],
    ),
    "sandcastle": (
        "Children building a sandcastle on a sunny beach next to a striped umbrella.",
        ["beach", "sandcastle", "children", "umbrella", "sand", "summer", "vacation"],
    ),
    "cathedral": (
        "A gothic cathedral with twin spires rising over a stone square in the morning light.",
        ["cathedral", "church", "gothic", "spires", "architecture", "square", "old town"],
    ),
    "pizza": (
        "A pizza with melted cheese and basil on a wooden table.",
        ["pizza", "food", "cheese", "restaurant", "italian", "table"],
    ),
    "red-car": (
        "A red sports car driving down a city street.",
        ["car", "red", "sports car", "road", "vehicle", "speed", "city"],
    ),
    "city-night": (
        "A city skyline at night with lit skyscrapers.",
        ["city", "night", "skyscrapers", "lights", "skyline", "urban", "buildings"],
    ),
    "roses": (
        "A bouquet of red roses in a glass vase on a table.",
        ["roses", "bouquet", "vase", "red", "flowers", "table", "romantic"],
    ),
}


@pytest.fixture
async def photos(alice: Tenant, worker: AsyncClient) -> dict[str, int]:
    assert _CACHE_DIR is not None
    embedder = FastEmbedEmbedder(model=MODEL, cache_dir=_CACHE_DIR)
    return {
        slug: await _add_image(
            alice, worker, embedder, filename=f"{slug}.png", tags=tags, description=description
        )
        for slug, (description, tags) in PHOTOS.items()
    }


@pytest.fixture
def real_model_settings(stack_settings: Settings) -> Settings:
    assert _CACHE_DIR is not None
    return stack_settings.model_copy(
        update={
            "EMBEDDER": "fastembed",
            "EMBEDDING_MODEL": MODEL,
            "EMBEDDING_CACHE_DIR": _CACHE_DIR,
        }
    )


def _slugs(photos: dict[str, int], items: list[dict[str, Any]]) -> list[str]:
    slug_by_id = {image_id: slug for slug, image_id in photos.items()}
    return [slug_by_id[item["id"]] for item in items]


@needs_model
async def test_puppy_finds_the_dogs_through_the_lazily_loaded_model(
    real_model_settings: Settings, alice: Tenant, photos: dict[str, int]
) -> None:
    async with _api(real_model_settings, "lazy") as api:
        found = _slugs(photos, await _search(api, alice, "puppy"))

    # The photo tagged "puppy" comes first, then the other dogs by meaning. Nothing else shows.
    assert found[0] == "golden-puppy"
    assert {"labrador", "beagles", "husky"} <= set(found)
    assert set(found) <= {"golden-puppy", "labrador", "beagles", "husky"}


@needs_model
async def test_seaside_finds_the_beaches(
    real_model_settings: Settings, alice: Tenant, photos: dict[str, int]
) -> None:
    async with _api(real_model_settings, "lazy") as api:
        found = _slugs(photos, await _search(api, alice, "seaside"))

    assert {"beach-sunset", "palm-beach", "sandcastle"} <= set(found)
    assert not {"cathedral", "pizza", "red-car", "roses"} & set(found)


@needs_model
async def test_the_words_still_rank_first_and_a_nonsense_query_finds_nothing(
    real_model_settings: Settings, alice: Tenant, photos: dict[str, int]
) -> None:
    async with _api(real_model_settings, "lazy") as api:
        beach = _slugs(photos, await _search(api, alice, "beach"))
        nothing = await _search(api, alice, "xylophone")

    # Every image with the tag "beach" ranks before any image found by meaning alone.
    assert set(beach[:3]) == {"beach-sunset", "palm-beach", "sandcastle"}
    assert nothing == []


@needs_model
async def test_with_the_embedder_off_puppy_finds_only_the_tagged_photo(
    stack_settings: Settings, alice: Tenant, photos: dict[str, int]
) -> None:
    async with _api(stack_settings, None) as api:
        found = _slugs(photos, await _search(api, alice, "puppy"))

    assert found == ["golden-puppy"]
