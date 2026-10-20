"""Duplicate uploads and bumps through the API against the local stack, with real users from Auth.

User A uploads X, then Y, then X again: the answers are 201, 201 and 200, X keeps its id and its
single row, and X comes first in A's gallery. User B uploads the same X and gets a copy of their
own, and every lookup stays inside the caller's images.

Start the stack with `make dev-db`, then export its values and run the tests:

    set -a; eval "$(supabase status -o env)"; set +a
    cd api && uv run pytest -m integration tests/integration/test_dedupe_api.py

The secret key is only used to delete the test users at the end. The images are deleted through the
API first, which removes their stored objects too.
"""

import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from io import BytesIO
from uuid import UUID

import httpx
import pytest
from PIL import Image

from gallery.api import deps
from gallery.config import Settings

pytestmark = pytest.mark.integration

# conftest.py clears the settings variables before every test, so read the stack's values now,
# while the tests are being collected.
_ENVIRONMENT = dict(os.environ)


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
    headers: dict[str, str]


async def _tenants(stack_settings: Settings, count: int) -> AsyncIterator[list[Tenant]]:
    async with httpx.AsyncClient(base_url=stack_settings.SUPABASE_URL) as http:
        tenants: list[Tenant] = []
        try:
            for _ in range(count):
                response = await http.post(
                    "/auth/v1/signup",
                    headers={"apikey": stack_settings.SUPABASE_PUBLISHABLE_KEY},
                    json={
                        "email": f"{uuid.uuid4().hex}@example.test",
                        "password": "test-password-123",
                    },
                )
                response.raise_for_status()
                body = response.json()
                tenants.append(
                    Tenant(
                        id=UUID(body["user"]["id"]),
                        headers={"Authorization": f"Bearer {body['access_token']}"},
                    )
                )
            yield tenants
        finally:
            secret_key = stack_settings.SUPABASE_SECRET_KEY
            assert secret_key is not None
            for tenant in tenants:
                deleted = await http.delete(
                    f"/auth/v1/admin/users/{tenant.id}",
                    headers={"apikey": secret_key, "Authorization": f"Bearer {secret_key}"},
                )
                # A user left behind would keep their images in the shared database.
                deleted.raise_for_status()


@pytest.fixture
async def alice_and_bob(stack_settings: Settings) -> AsyncIterator[tuple[Tenant, Tenant]]:
    async for tenants in _tenants(stack_settings, 2):
        alice, bob = tenants
        yield alice, bob


@pytest.fixture
async def api(stack_settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    # gallery.main builds the app when it is imported, and that reads the settings. Import it
    # here, after conftest's autouse fixture has set them, instead of at module level.
    from gallery.main import create_app

    app = create_app()
    app.dependency_overrides[deps.get_settings] = lambda: stack_settings
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60) as http:
        yield http


def a_png(color: str) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (64, 48), color).save(buffer, format="PNG")
    return buffer.getvalue()


X = a_png("crimson")
Y = a_png("royalblue")
Z = a_png("seagreen")


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def upload(
    api: httpx.AsyncClient, tenant: Tenant, data: bytes, filename: str
) -> httpx.Response:
    return await api.post(
        "/api/v1/images",
        headers=tenant.headers,
        files={"file": (filename, data, "image/png")},
    )


async def gallery_ids(api: httpx.AsyncClient, tenant: Tenant) -> list[int]:
    response = await api.get("/api/v1/images", params={"limit": 50}, headers=tenant.headers)
    assert response.status_code == 200
    return [item["id"] for item in response.json()["items"]]


async def lookup(api: httpx.AsyncClient, tenant: Tenant, *datas: bytes) -> dict[str, int]:
    response = await api.post(
        "/api/v1/images/lookup",
        headers=tenant.headers,
        json={"content_sha256": [sha256_of(data) for data in datas]},
    )
    assert response.status_code == 200
    return response.json()["matches"]


async def test_a_second_upload_of_the_same_file_bumps_it_and_stores_nothing_new(
    api: httpx.AsyncClient, alice_and_bob: tuple[Tenant, Tenant]
) -> None:
    alice, bob = alice_and_bob
    uploaded: list[tuple[Tenant, int]] = []
    try:
        first_x = await upload(api, alice, X, "x.png")
        y = await upload(api, alice, Y, "y.png")
        second_x = await upload(api, alice, X, "x-again.png")

        assert [first_x.status_code, y.status_code, second_x.status_code] == [201, 201, 200]
        x_id, y_id = first_x.json()["id"], y.json()["id"]
        uploaded += [(alice, x_id), (alice, y_id)]
        assert second_x.json()["id"] == x_id
        # The answer is the existing image, not a renamed copy of it.
        assert second_x.json()["filename"] == "x.png"
        assert second_x.json()["thumbnail_url"]
        # One row for X, and it is first.
        assert await gallery_ids(api, alice) == [x_id, y_id]

        # B has no X yet, so the same bytes are a new image of their own.
        bobs_x = await upload(api, bob, X, "x.png")
        assert bobs_x.status_code == 201
        assert bobs_x.json()["id"] not in (x_id, y_id)
        uploaded.append((bob, bobs_x.json()["id"]))
        assert await gallery_ids(api, bob) == [bobs_x.json()["id"]]
        assert await gallery_ids(api, alice) == [x_id, y_id]

        # Each lookup sees the caller's images only.
        assert await lookup(api, alice, X, Y, Z) == {sha256_of(X): x_id, sha256_of(Y): y_id}
        assert await lookup(api, bob, X, Y, Z) == {sha256_of(X): bobs_x.json()["id"]}
    finally:
        for owner, image_id in uploaded:
            await api.delete(f"/api/v1/images/{image_id}", headers=owner.headers)


async def test_the_bump_route_moves_an_image_up_and_only_for_its_owner(
    api: httpx.AsyncClient, alice_and_bob: tuple[Tenant, Tenant]
) -> None:
    alice, bob = alice_and_bob
    x = (await upload(api, alice, X, "x.png")).json()["id"]
    y = (await upload(api, alice, Y, "y.png")).json()["id"]
    try:
        assert await gallery_ids(api, alice) == [y, x]

        bumped = await api.post(f"/api/v1/images/{x}/bump", headers=alice.headers)
        stranger = await api.post(f"/api/v1/images/{x}/bump", headers=bob.headers)
        missing = await api.post("/api/v1/images/999999999/bump", headers=alice.headers)
        anonymous = await api.post(f"/api/v1/images/{x}/bump")

        assert bumped.status_code == 200
        assert bumped.json()["id"] == x
        assert bumped.json()["thumbnail_url"]
        assert await gallery_ids(api, alice) == [x, y]
        for refused in (stranger, missing):
            assert refused.status_code == 404
            assert refused.json() == {"error": {"code": "not_found", "message": "Image not found."}}
        assert anonymous.status_code == 401
        assert await gallery_ids(api, bob) == []
    finally:
        for image_id in (x, y):
            await api.delete(f"/api/v1/images/{image_id}", headers=alice.headers)
