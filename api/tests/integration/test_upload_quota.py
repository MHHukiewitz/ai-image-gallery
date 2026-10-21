"""The daily quota against the local stack, with a real user from Auth: deleting an image keeps its
slot used, because the quota counts `upload_log` and a log row outlives its image.

Start the stack with `make dev-db`, then export its values and run the tests:

    set -a; eval "$(supabase status -o env)"; set +a
    cd api && uv run pytest -m integration tests/integration/test_upload_quota.py

The secret key is only used to delete the test user at the end. The images are deleted through the
API first, which removes their stored objects too.
"""

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import UUID

import httpx
import pytest
from PIL import Image

from gallery.adapters.supabase_client import close_client, user_client
from gallery.adapters.supabase_images import SupabaseImageRepo
from gallery.api import deps
from gallery.config import Settings

pytestmark = pytest.mark.integration

# conftest.py clears the settings variables before every test, so read the stack's values now,
# while the tests are being collected.
_ENVIRONMENT = dict(os.environ)

QUOTA = 2


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
        UPLOAD_DAILY_QUOTA=QUOTA,
    )


@dataclass(frozen=True)
class Tenant:
    id: UUID
    access_token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


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
        user = Tenant(id=UUID(body["user"]["id"]), access_token=body["access_token"])
        try:
            yield user
        finally:
            secret_key = stack_settings.SUPABASE_SECRET_KEY
            assert secret_key is not None
            deleted = await http.delete(
                f"/auth/v1/admin/users/{user.id}",
                headers={"apikey": secret_key, "Authorization": f"Bearer {secret_key}"},
            )
            # A user left behind would keep their rows in the shared database.
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


def a_png(color: str) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (64, 48), color).save(buffer, format="PNG")
    return buffer.getvalue()


async def upload(api: httpx.AsyncClient, tenant: Tenant, data: bytes) -> httpx.Response:
    return await api.post(
        "/api/v1/images", headers=tenant.headers, files={"file": ("photo.png", data, "image/png")}
    )


async def uploads_in_the_window(stack_settings: Settings, tenant: Tenant) -> int:
    client = await user_client(stack_settings, tenant.access_token)
    try:
        repo = SupabaseImageRepo(client)
        return await repo.count_uploaded_since(datetime.now(UTC) - timedelta(hours=24))
    finally:
        await close_client(client)


async def test_an_upload_a_delete_and_a_second_upload_count_as_two(
    api: httpx.AsyncClient, tenant: Tenant, stack_settings: Settings
) -> None:
    kept: list[int] = []
    try:
        first = await upload(api, tenant, a_png("crimson"))
        assert first.status_code == 201
        deleted = await api.delete(f"/api/v1/images/{first.json()['id']}", headers=tenant.headers)
        assert deleted.status_code == 204
        second = await upload(api, tenant, a_png("royalblue"))
        assert second.status_code == 201
        kept.append(second.json()["id"])

        # The gallery holds one image, but both uploads stay in the window.
        assert await uploads_in_the_window(stack_settings, tenant) == 2

        # So at a quota of 2 the slot the delete would once have freed is still used.
        third = await upload(api, tenant, a_png("seagreen"))
        assert third.status_code == 429
        assert third.json()["error"]["code"] == "quota_exceeded"
        # A re-upload of an image the user still has is a bump, which stays free at the quota.
        again = await upload(api, tenant, a_png("royalblue"))
        assert again.status_code == 200
        assert await uploads_in_the_window(stack_settings, tenant) == 2
    finally:
        for image_id in kept:
            await api.delete(f"/api/v1/images/{image_id}", headers=tenant.headers)
