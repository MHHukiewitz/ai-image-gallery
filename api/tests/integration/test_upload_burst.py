"""A burst of uploads through the API against the local stack, as one user signed up through Auth.

This is the load check for the Storage 429 incident: 30 images go in at once, and every one has
to come back as a 201 that the grid can show with a working thumbnail. The local stack's Storage
has a bigger connection pool than the free tier's, so it seldom answers 429 itself. What the test
checks is that nothing in the API breaks when many uploads overlap, and the unit tests cover the
retries.

Start the stack with `make dev-db`, then export its values and run the test, with `-s` to see the
summary it prints:

    set -a; eval "$(supabase status -o env)"; set +a
    cd api && uv run pytest -m integration -s tests/integration/test_upload_burst.py

The secret key is only used to delete the test user at the end. The images are deleted through
the API first, which removes their stored objects too.
"""

import asyncio
import logging
import os
import time
import uuid
from collections import Counter
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

BURST = 30

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


@pytest.fixture
async def tenant(stack_settings: Settings) -> AsyncIterator[Tenant]:
    async with httpx.AsyncClient(base_url=stack_settings.SUPABASE_URL) as http:
        response = await http.post(
            "/auth/v1/signup",
            headers={"apikey": stack_settings.SUPABASE_PUBLISHABLE_KEY},
            json={"email": f"{uuid.uuid4().hex}@example.test", "password": "test-password-123"},
        )
        response.raise_for_status()
        body = response.json()
        user_id = UUID(body["user"]["id"])
        try:
            yield Tenant(id=user_id, headers={"Authorization": f"Bearer {body['access_token']}"})
        finally:
            secret_key = stack_settings.SUPABASE_SECRET_KEY
            assert secret_key is not None
            deleted = await http.delete(
                f"/auth/v1/admin/users/{user_id}",
                headers={"apikey": secret_key, "Authorization": f"Bearer {secret_key}"},
            )
            # A user left behind would keep their images in the shared database, so fail loudly.
            deleted.raise_for_status()


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


def a_jpeg(index: int) -> bytes:
    """A photo-sized JPEG whose bytes differ per index, so no two uploads share a hash."""
    image = Image.effect_noise((800, 600), 40).convert("RGB")
    image = Image.blend(image, Image.new("RGB", image.size, (index * 8 % 256, 90, 160)), 0.5)
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


async def test_a_burst_of_uploads_all_succeed_and_every_thumbnail_loads(
    api: httpx.AsyncClient, tenant: Tenant, caplog: pytest.LogCaptureFixture
) -> None:
    photos = [a_jpeg(index) for index in range(BURST)]
    uploaded_ids: list[int] = []
    caplog.set_level(logging.WARNING, logger="gallery")

    async def upload(index: int) -> httpx.Response:
        return await api.post(
            "/api/v1/images",
            headers=tenant.headers,
            files={"file": (f"burst-{index}.jpg", photos[index], "image/jpeg")},
        )

    started = time.perf_counter()
    try:
        responses = await asyncio.gather(*(upload(index) for index in range(BURST)))
        elapsed = time.perf_counter() - started
        uploaded_ids = [r.json()["id"] for r in responses if r.status_code == 201]
        upload_statuses = Counter(response.status_code for response in responses)

        # The grid asks for a page of up to 50 and shows each item's thumbnail_url.
        listing = await api.get("/api/v1/images", params={"limit": 50}, headers=tenant.headers)
        items = listing.json()["items"]
        async with httpx.AsyncClient(timeout=30) as http:
            fetches = await asyncio.gather(
                *(http.get(item["thumbnail_url"]) for item in items if item["thumbnail_url"])
            )
        thumbnail_statuses = Counter(fetch.status_code for fetch in fetches)
        retries = sum(1 for record in caplog.records if record.getMessage().endswith("retrying"))

        # The load check's report, shown with `pytest -s`.
        print(
            f"\nupload statuses: {dict(upload_statuses)} in {elapsed:.1f}s; "
            f"grid items: {len(items)}, "
            f"empty thumbnail urls: {sum(1 for item in items if not item['thumbnail_url'])}; "
            f"thumbnail fetch statuses: {dict(thumbnail_statuses)}; storage retries: {retries}"
        )
        assert upload_statuses == {201: BURST}
        assert listing.status_code == 200
        assert len(items) == BURST
        assert all(item["thumbnail_url"] for item in items)
        assert thumbnail_statuses == {200: BURST}
    finally:
        # Delete through the API so the stored objects go too; deleting the user would leave them.
        await asyncio.gather(
            *(
                api.delete(f"/api/v1/images/{image_id}", headers=tenant.headers)
                for image_id in uploaded_ids
            )
        )
