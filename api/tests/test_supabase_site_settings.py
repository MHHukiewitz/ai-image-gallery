"""SupabaseSiteSettingsRepo against mocked PostgREST HTTP and the real SDK.

Who may read and change what is covered by supabase/tests/signup_switch_test.sql. These check
what the adapter asks PostgREST for, how it reads the answer, and how a refusal becomes
ForbiddenError.
"""

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import httpx
import pytest
import respx
from postgrest import APIError

from gallery.adapters.supabase_client import AsyncClient, anon_client, close_client, user_client
from gallery.adapters.supabase_site_settings import SupabaseSiteSettingsRepo
from gallery.config import Settings
from gallery.domain.errors import ForbiddenError
from gallery.domain.models import SiteSettings

BASE = "http://localhost:54321/rest/v1"
SETTINGS_ROW = {"allow_signups": False, "updated_at": "2026-10-23T09:00:00+00:00"}


class Pauses:
    """Stands in for `asyncio.sleep` and `random.uniform`, so retries run without waiting."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def uniform(self, low: float, high: float) -> float:
        return high


@pytest.fixture
def postgrest() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    client = await user_client(settings, "test-token")
    yield client
    await close_client(client)


@pytest.fixture
def pauses() -> Pauses:
    return Pauses()


@pytest.fixture
def repo(client: AsyncClient, pauses: Pauses) -> SupabaseSiteSettingsRepo:
    return SupabaseSiteSettingsRepo(client, sleep=pauses.sleep, uniform=pauses.uniform)


def not_admin() -> httpx.Response:
    """How PostgREST answers get_site_settings or set_allow_signups for anyone but an admin."""
    return httpx.Response(
        403,
        json={"code": "PT403", "message": "Only an admin", "details": None, "hint": None},
    )


@pytest.mark.parametrize(("payload", "expected"), [(True, True), (False, False), (None, False)])
async def test_signups_allowed_reads_the_bare_boolean(
    repo: SupabaseSiteSettingsRepo,
    postgrest: respx.MockRouter,
    payload: bool | None,
    expected: bool,
) -> None:
    postgrest.post(f"{BASE}/rpc/signups_allowed").mock(
        return_value=httpx.Response(200, json=payload)
    )

    assert await repo.signups_allowed() is expected


async def test_signups_allowed_asks_as_anon_without_a_user_token(
    settings: Settings, postgrest: respx.MockRouter
) -> None:
    route = postgrest.post(f"{BASE}/rpc/signups_allowed").mock(
        return_value=httpx.Response(200, json=True)
    )
    client = await anon_client(settings)
    try:
        assert await SupabaseSiteSettingsRepo(client).signups_allowed() is True
    finally:
        await close_client(client)

    request = route.calls.last.request
    assert request.headers["apikey"] == "test-key"
    assert "test-token" not in request.headers.get("authorization", "")


@pytest.mark.parametrize("admin", [True, False])
async def test_caller_is_admin_reads_the_bare_boolean(
    repo: SupabaseSiteSettingsRepo, postgrest: respx.MockRouter, admin: bool
) -> None:
    route = postgrest.post(f"{BASE}/rpc/is_admin").mock(
        return_value=httpx.Response(200, json=admin)
    )

    assert await repo.caller_is_admin() is admin
    assert route.calls.last.request.headers["authorization"] == "Bearer test-token"


async def test_get_maps_the_row(
    repo: SupabaseSiteSettingsRepo, postgrest: respx.MockRouter
) -> None:
    postgrest.post(f"{BASE}/rpc/get_site_settings").mock(
        return_value=httpx.Response(200, json=[SETTINGS_ROW])
    )

    assert await repo.get() == SiteSettings(
        allow_signups=False, updated_at=datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
    )


async def test_set_allow_signups_sends_the_switch(
    repo: SupabaseSiteSettingsRepo, postgrest: respx.MockRouter
) -> None:
    route = postgrest.post(f"{BASE}/rpc/set_allow_signups").mock(
        return_value=httpx.Response(200, json=[SETTINGS_ROW])
    )

    settings = await repo.set_allow_signups(False)

    assert json.loads(route.calls.last.request.content) == {"p_allow": False}
    assert settings.allow_signups is False


@pytest.mark.parametrize("rpc", ["get_site_settings", "set_allow_signups"])
async def test_a_non_admin_gets_forbidden(
    repo: SupabaseSiteSettingsRepo, postgrest: respx.MockRouter, rpc: str
) -> None:
    route = postgrest.post(f"{BASE}/rpc/{rpc}").mock(return_value=not_admin())

    with pytest.raises(ForbiddenError):
        if rpc == "get_site_settings":
            await repo.get()
        else:
            await repo.set_allow_signups(True)
    # A refusal is an answer, not a hiccup: no retry.
    assert route.call_count == 1


async def test_a_gateways_own_403_is_not_read_as_not_admin(
    repo: SupabaseSiteSettingsRepo, postgrest: respx.MockRouter
) -> None:
    postgrest.post(f"{BASE}/rpc/get_site_settings").mock(
        return_value=httpx.Response(403, text="<html>Forbidden</html>")
    )

    with pytest.raises(APIError):
        await repo.get()


async def test_a_transient_failure_is_retried(
    repo: SupabaseSiteSettingsRepo, postgrest: respx.MockRouter, pauses: Pauses
) -> None:
    route = postgrest.post(f"{BASE}/rpc/set_allow_signups").mock(
        side_effect=[
            httpx.Response(504, text="Gateway Time-out"),
            httpx.Response(200, json=[SETTINGS_ROW]),
        ]
    )

    assert (await repo.set_allow_signups(False)).allow_signups is False
    assert route.call_count == 2
    assert len(pauses.sleeps) == 1
