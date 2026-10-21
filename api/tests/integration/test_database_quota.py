"""The daily quota in the database, against the local stack, with a real user from Auth: a
signed-in client that skips the API and inserts into `images` through PostgREST with its own JWT is
refused once it has 300 uploads in 24 hours (#95).

Start the stack with `make dev-db`, then export its values and run the tests:

    set -a; eval "$(supabase status -o env)"; set +a
    cd api && uv run pytest -m integration tests/integration/test_database_quota.py

The images have no stored objects: nothing here reads them. The secret key is only used to delete
the test user at the end, which removes its rows.
"""

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import httpx
import pytest
from postgrest import CountMethod

from gallery.adapters.supabase_client import AsyncClient, close_client, user_client
from gallery.adapters.supabase_images import SupabaseImageRepo
from gallery.config import Settings
from gallery.domain.errors import QuotaExceededError
from gallery.domain.models import NewImage

pytestmark = pytest.mark.integration

# conftest.py clears the settings variables before every test, so read the stack's values now,
# while the tests are being collected.
_ENVIRONMENT = dict(os.environ)

# The limit the migration seeds.
DATABASE_LIMIT = 300
REFUSAL = "You've reached today's upload limit of 300 images. Try again tomorrow."


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
async def client(stack_settings: Settings, tenant: Tenant) -> AsyncIterator[AsyncClient]:
    client = await user_client(stack_settings, tenant.access_token)
    yield client
    await close_client(client)


def image_row(owner: UUID, slug: str) -> dict[str, object]:
    """One row as a client would POST it to /rest/v1/images."""
    folder = f"{owner}/{slug}"
    return {
        "user_id": str(owner),
        "filename": f"{slug}.jpg",
        "original_path": f"{folder}/original.jpg",
        "thumbnail_path": f"{folder}/thumb.webp",
        "mime_type": "image/jpeg",
        "size_bytes": 1024,
        "width": 640,
        "height": 480,
        "content_sha256": "a" * 64,
    }


async def stored_images(client: AsyncClient) -> int:
    response = (
        await client.table("images").select("id", count=CountMethod.exact, head=True).execute()
    )
    return response.count or 0


async def test_a_client_that_skips_the_api_is_refused_past_the_limit(
    stack_settings: Settings, tenant: Tenant, client: AsyncClient
) -> None:
    # One bulk request fills the quota, as a client skipping the API would send it.
    rows = [image_row(tenant.id, f"fill-{n}") for n in range(DATABASE_LIMIT)]
    filled = await client.table("images").insert(rows).execute()
    assert len(filled.data) == DATABASE_LIMIT

    # The 301st image, posted straight to PostgREST with the user's own JWT.
    async with httpx.AsyncClient(base_url=f"{stack_settings.SUPABASE_URL}/rest/v1") as http:
        refused = await http.post(
            "/images",
            headers={
                "apikey": stack_settings.SUPABASE_PUBLISHABLE_KEY,
                "Authorization": f"Bearer {tenant.access_token}",
                "Prefer": "return=minimal",
            },
            json=image_row(tenant.id, "one-too-many"),
        )

    assert refused.status_code == 429
    assert refused.json()["code"] == "PT429"
    assert refused.json()["message"] == REFUSAL
    assert await stored_images(client) == DATABASE_LIMIT


async def test_one_bulk_request_past_the_limit_is_refused_whole(
    tenant: Tenant, client: AsyncClient
) -> None:
    # A check on each row alone would let all of these in: none of them sees the others.
    rows = [image_row(tenant.id, f"bulk-{n}") for n in range(DATABASE_LIMIT + 1)]

    with pytest.raises(Exception) as refused:
        await client.table("images").insert(rows).execute()

    assert getattr(refused.value, "code", None) == "PT429"
    assert await stored_images(client) == 0


async def test_the_repo_turns_the_refusal_into_quota_exceeded(
    tenant: Tenant, client: AsyncClient
) -> None:
    await (
        client.table("images")
        .insert([image_row(tenant.id, f"fill-{n}") for n in range(DATABASE_LIMIT)])
        .execute()
    )
    repo = SupabaseImageRepo(client)
    folder = f"{tenant.id}/next"

    with pytest.raises(QuotaExceededError) as refused:
        await repo.insert(
            NewImage(
                user_id=tenant.id,
                filename="next.jpg",
                original_path=f"{folder}/original.jpg",
                thumbnail_path=f"{folder}/thumb.webp",
                mime_type="image/jpeg",
                size_bytes=1024,
                width=640,
                height=480,
                content_sha256="b" * 64,
            )
        )

    assert refused.value.message == REFUSAL
    assert refused.value.retry_after_seconds == 24 * 60 * 60
    # Nothing was left behind: no image row, so no metadata row to clean up either.
    assert await stored_images(client) == DATABASE_LIMIT
