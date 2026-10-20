"""Re-uploading a drop of already-uploaded images through the API against the local stack.

The web client checks a drop with one `POST /images/lookup` for all the hashes and one
`POST /images/bump` for all the matches, instead of one bump request per image. These tests run
that flow with 40 duplicates, count the requests, time them, and check the gallery order the way
the grid reads it: the last image of the drop first. They use real users from Auth.

Start the stack with `make dev-db`, then export its values and run the tests (add `-s` to see the
timings):

    set -a; eval "$(supabase status -o env)"; set +a
    cd api && uv run pytest -m integration -s tests/integration/test_reupload_batch.py

The secret key is only used to delete the test users at the end. The images are deleted through the
API first, which removes their stored objects too.
"""

import hashlib
import os
import random
import time
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

DUPLICATES = 40
# "A few seconds" for the whole re-upload check on a laptop.
BUDGET_SECONDS = 5.0

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
def requests_made() -> list[tuple[str, str]]:
    """Every request the API client sends, as (method, path), for counting."""
    return []


@pytest.fixture
async def api(
    stack_settings: Settings, requests_made: list[tuple[str, str]]
) -> AsyncIterator[httpx.AsyncClient]:
    # gallery.main builds the app when it is imported, and that reads the settings. Import it
    # here, after conftest's autouse fixture has set them, instead of at module level.
    from gallery.main import create_app

    async def record(request: httpx.Request) -> None:
        requests_made.append((request.method, request.url.path))

    app = create_app()
    app.dependency_overrides[deps.get_settings] = lambda: stack_settings
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        timeout=60,
        event_hooks={"request": [record]},
    ) as http:
        yield http


def a_png(index: int) -> bytes:
    """A small PNG of its own: a distinct colour per index, so every image has its own hash."""
    buffer = BytesIO()
    color = ((index * 6) % 256, (index * 11) % 256, (index * 17) % 256)
    Image.new("RGB", (64, 48), color).save(buffer, format="PNG")
    return buffer.getvalue()


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def upload(api: httpx.AsyncClient, tenant: Tenant, data: bytes, filename: str) -> int:
    response = await api.post(
        "/api/v1/images",
        headers=tenant.headers,
        files={"file": (filename, data, "image/png")},
    )
    assert response.status_code == 201
    image_id: int = response.json()["id"]
    return image_id


async def gallery_ids(api: httpx.AsyncClient, tenant: Tenant) -> list[int]:
    response = await api.get("/api/v1/images", params={"limit": 50}, headers=tenant.headers)
    assert response.status_code == 200
    return [item["id"] for item in response.json()["items"]]


async def test_a_reupload_of_40_duplicates_is_one_lookup_and_one_bump(
    api: httpx.AsyncClient,
    alice_and_bob: tuple[Tenant, Tenant],
    requests_made: list[tuple[str, str]],
) -> None:
    alice, _ = alice_and_bob
    files = [a_png(index) for index in range(DUPLICATES)]
    uploaded: list[int] = []
    try:
        started = time.perf_counter()
        for index, data in enumerate(files):
            uploaded.append(await upload(api, alice, data, f"photo-{index}.png"))
        upload_seconds = time.perf_counter() - started
        id_of = {sha256_of(data): image_id for data, image_id in zip(files, uploaded, strict=True)}

        # The user drops the same 40 files again, in another order.
        drop = list(files)
        random.Random(76).shuffle(drop)
        assert drop != files
        hashes = [sha256_of(data) for data in drop]
        requests_made.clear()

        started = time.perf_counter()
        lookup = await api.post(
            "/api/v1/images/lookup", headers=alice.headers, json={"content_sha256": hashes}
        )
        looked_up = time.perf_counter()
        matches = lookup.json()["matches"]
        bump = await api.post(
            "/api/v1/images/bump",
            headers=alice.headers,
            json={"image_ids": [matches[digest] for digest in hashes]},
        )
        bumped = time.perf_counter()

        lookup_ms = (looked_up - started) * 1000
        bump_ms = (bumped - looked_up) * 1000
        total_seconds = bumped - started
        expected_order = [id_of[digest] for digest in hashes]

        # One lookup and one bump, nothing else.
        assert requests_made == [
            ("POST", "/api/v1/images/lookup"),
            ("POST", "/api/v1/images/bump"),
        ]
        assert lookup.status_code == 200
        assert len(matches) == DUPLICATES
        assert bump.status_code == 200
        items = bump.json()["items"]
        # Every image moved, answered in the order of the drop, each with a signed thumbnail.
        assert [item["id"] for item in items] == expected_order
        assert all(item["thumbnail_url"] for item in items)
        # And the grid reads them back with the last image of the drop first.
        assert await gallery_ids(api, alice) == list(reversed(expected_order))
        assert total_seconds < BUDGET_SECONDS

        # For comparison: what the web client did before, one bump request per image.
        single_started = time.perf_counter()
        for image_id in expected_order:
            single = await api.post(f"/api/v1/images/{image_id}/bump", headers=alice.headers)
            assert single.status_code == 200
        sequential_seconds = time.perf_counter() - single_started
        assert await gallery_ids(api, alice) == list(reversed(expected_order))

        total_ms = total_seconds * 1000
        sequential_ms = sequential_seconds * 1000
        print(
            f"\nre-upload of {DUPLICATES} duplicates: lookup {lookup_ms:.0f} ms, "
            f"bump {bump_ms:.0f} ms, total {total_ms:.0f} ms in 2 requests. "
            f"Before: {DUPLICATES} bump requests in {sequential_ms:.0f} ms. "
            f"(The first upload of the {DUPLICATES} files took {upload_seconds:.1f} s.)"
        )
    finally:
        for image_id in uploaded:
            await api.delete(f"/api/v1/images/{image_id}", headers=alice.headers)


async def test_the_batch_bump_only_moves_the_callers_images(
    api: httpx.AsyncClient, alice_and_bob: tuple[Tenant, Tenant]
) -> None:
    alice, bob = alice_and_bob
    uploaded: list[tuple[Tenant, int]] = []
    try:
        alices_a = await upload(api, alice, a_png(1), "a.png")
        alices_b = await upload(api, alice, a_png(2), "b.png")
        bobs_c = await upload(api, bob, a_png(3), "c.png")
        bobs_d = await upload(api, bob, a_png(4), "d.png")
        uploaded += [(alice, alices_a), (alice, alices_b), (bob, bobs_c), (bob, bobs_d)]

        # Bob names his own two, one of Alice's and an id that does not exist.
        response = await api.post(
            "/api/v1/images/bump",
            headers=bob.headers,
            json={"image_ids": [bobs_c, alices_a, 999_999_999, bobs_d]},
        )

        assert response.status_code == 200
        assert [item["id"] for item in response.json()["items"]] == [bobs_c, bobs_d]
        assert await gallery_ids(api, bob) == [bobs_d, bobs_c]
        # Alice's gallery did not move.
        assert await gallery_ids(api, alice) == [alices_b, alices_a]

        only_foreign = await api.post(
            "/api/v1/images/bump", headers=bob.headers, json={"image_ids": [alices_a]}
        )
        assert only_foreign.status_code == 200
        assert only_foreign.json() == {"items": []}
        assert await gallery_ids(api, alice) == [alices_b, alices_a]
    finally:
        for owner, image_id in uploaded:
            await api.delete(f"/api/v1/images/{image_id}", headers=owner.headers)


async def test_the_batch_bump_rejects_too_many_ids_and_anonymous_calls(
    api: httpx.AsyncClient, alice_and_bob: tuple[Tenant, Tenant]
) -> None:
    alice, _ = alice_and_bob

    too_many = await api.post(
        "/api/v1/images/bump", headers=alice.headers, json={"image_ids": list(range(1, 52))}
    )
    anonymous = await api.post("/api/v1/images/bump", json={"image_ids": [1]})

    assert too_many.status_code == 422
    assert too_many.json()["error"]["code"] == "validation_error"
    assert anonymous.status_code == 401
