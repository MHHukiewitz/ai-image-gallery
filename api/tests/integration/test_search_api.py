"""Search through the API against the local stack, with two real users signed up through Auth.

Start the stack with `make dev-db`, then export its values and run the tests:

    set -a; eval "$(supabase status -o env)"; set +a
    cd api && uv run pytest -m integration

The secret key is required, unlike in the adapter tests. It stores each image's analysis the way
the worker does, through `complete_analysis_job`, and it deletes the test users at the end, which
removes their images too.
"""

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import pytest

from gallery.adapters.supabase_client import (
    AsyncClient,
    close_client,
    service_client,
    user_client,
)
from gallery.adapters.supabase_images import SupabaseImageRepo
from gallery.api import deps
from gallery.config import Settings
from gallery.domain.models import NewImage

pytestmark = pytest.mark.integration

# conftest.py clears the settings variables before every test, so read the stack's values now,
# while the tests are being collected.
_ENVIRONMENT = dict(os.environ)

# Stored as the analysis of every seeded image. Only the analyzer_id names what made the tags.
_ANALYZER_ID = "test:fake:1"


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


async def _sign_up(settings: Settings) -> tuple[UUID, str]:
    async with httpx.AsyncClient(base_url=settings.SUPABASE_URL) as http:
        response = await http.post(
            "/auth/v1/signup",
            headers={"apikey": settings.SUPABASE_PUBLISHABLE_KEY},
            json={"email": f"{uuid.uuid4().hex}@example.test", "password": "test-password-123"},
        )
    response.raise_for_status()
    body = response.json()
    return UUID(body["user"]["id"]), body["access_token"]


async def _delete_user(settings: Settings, user_id: UUID) -> None:
    secret_key = settings.SUPABASE_SECRET_KEY
    assert secret_key is not None
    async with httpx.AsyncClient(base_url=settings.SUPABASE_URL) as http:
        response = await http.delete(
            f"/auth/v1/admin/users/{user_id}",
            headers={"apikey": secret_key, "Authorization": f"Bearer {secret_key}"},
        )
    # A user left behind would keep their images in the shared database, so fail loudly.
    response.raise_for_status()


async def _tenant(settings: Settings) -> AsyncIterator[Tenant]:
    user_id, access_token = await _sign_up(settings)
    client = await user_client(settings, access_token)
    try:
        yield Tenant(id=user_id, access_token=access_token, images=SupabaseImageRepo(client))
    finally:
        await close_client(client)
        await _delete_user(settings, user_id)


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


async def _add_analysed_image(
    owner: Tenant,
    worker: AsyncClient,
    *,
    filename: str,
    tags: list[str],
    description: str,
    colors: list[str],
) -> int:
    """Insert an image as the upload does, then store its analysis as the worker does."""
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
    # The trigger derives search_vector and colors_lab from these, so the search sees them.
    await worker.rpc(
        "complete_analysis_job",
        {
            "p_image_id": record.id,
            "p_tags": tags,
            "p_description": description,
            "p_colors": colors,
            "p_analyzer_id": _ANALYZER_ID,
        },
    ).execute()
    return record.id


@dataclass(frozen=True)
class Gallery:
    """Alice's seeded images. `exact` is inserted first, so the newest-first order would put `near`
    ahead of it. Only closeness puts `exact` first in a color search."""

    exact: int  # tags red and sunset, color #ff0000
    near: int  # tag sunset, color #cc0000: a close red, about 19 apart in CIE76
    blue: int  # tag sea, color #0000ff: far from red, and shares no tags with `exact`


@pytest.fixture
async def gallery(alice: Tenant, worker: AsyncClient) -> Gallery:
    exact = await _add_analysed_image(
        alice,
        worker,
        filename="exact-red.png",
        tags=["red", "sunset"],
        description="A red sunset over the sea.",
        colors=["#ff0000"],
    )
    near = await _add_analysed_image(
        alice,
        worker,
        filename="dark-red.png",
        tags=["sunset"],
        description="Dusk over the hills.",
        colors=["#cc0000"],
    )
    blue = await _add_analysed_image(
        alice,
        worker,
        filename="blue-sea.png",
        tags=["sea"],
        description="Calm blue water.",
        colors=["#0000ff"],
    )
    return Gallery(exact=exact, near=near, blue=blue)


def _as(tenant: Tenant) -> dict[str, str]:
    return {"Authorization": f"Bearer {tenant.access_token}"}


def _ids(body: dict[str, Any]) -> list[int]:
    return [item["id"] for item in body["items"]]


async def test_a_text_search_finds_only_the_matching_images(
    api: httpx.AsyncClient, alice: Tenant, gallery: Gallery
) -> None:
    response = await api.get("/api/v1/images", params={"q": "red"}, headers=_as(alice))

    assert response.status_code == 200
    body = response.json()
    assert _ids(body) == [gallery.exact]
    assert body["next_offset"] is None
    assert isinstance(body["items"][0]["score"], float)
    assert body["items"][0]["score"] > 0


async def test_similar_to_excludes_the_target_and_keeps_the_related_image(
    api: httpx.AsyncClient, alice: Tenant, gallery: Gallery
) -> None:
    response = await api.get(
        "/api/v1/images", params={"similar_to": gallery.exact}, headers=_as(alice)
    )

    assert response.status_code == 200
    ids = _ids(response.json())
    assert gallery.exact not in ids
    # `near` shares a tag and a close color. `blue` shares no tag and has a far color, so it
    # scores below the 0.1 floor.
    assert ids == [gallery.near]


async def test_a_color_search_orders_the_closest_color_first(
    api: httpx.AsyncClient, alice: Tenant, gallery: Gallery
) -> None:
    response = await api.get("/api/v1/images", params={"color": "#ff0000"}, headers=_as(alice))

    assert response.status_code == 200
    body = response.json()
    assert _ids(body) == [gallery.exact, gallery.near]
    exact_score, near_score = (item["score"] for item in body["items"])
    assert exact_score == pytest.approx(1.0)
    assert exact_score > near_score > 0.1


async def test_a_search_pages_through_its_results(
    api: httpx.AsyncClient, alice: Tenant, gallery: Gallery
) -> None:
    # "sunset" matches both `exact` and `near`, so a page of one has a next page.
    first = await api.get("/api/v1/images", params={"q": "sunset", "limit": 1}, headers=_as(alice))
    second = await api.get(
        "/api/v1/images",
        params={"q": "sunset", "limit": 1, "offset": 1},
        headers=_as(alice),
    )

    assert first.json()["next_offset"] == 1
    assert second.json()["next_offset"] is None
    assert sorted(_ids(first.json()) + _ids(second.json())) == sorted([gallery.exact, gallery.near])


async def test_another_user_gets_404_for_a_similar_to_search(
    api: httpx.AsyncClient, bob: Tenant, gallery: Gallery
) -> None:
    response = await api.get(
        "/api/v1/images", params={"similar_to": gallery.exact}, headers=_as(bob)
    )

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "Image not found."}}


async def test_a_similar_to_search_for_a_missing_image_is_404(
    api: httpx.AsyncClient, alice: Tenant
) -> None:
    response = await api.get("/api/v1/images", params={"similar_to": 2**40}, headers=_as(alice))

    assert response.status_code == 404


async def test_another_user_gets_empty_text_and_color_results(
    api: httpx.AsyncClient, bob: Tenant, gallery: Gallery
) -> None:
    text = await api.get("/api/v1/images", params={"q": "red"}, headers=_as(bob))
    color = await api.get("/api/v1/images", params={"color": "#ff0000"}, headers=_as(bob))

    assert text.status_code == 200
    assert text.json() == {"items": [], "next_offset": None}
    assert color.status_code == 200
    assert color.json() == {"items": [], "next_offset": None}
