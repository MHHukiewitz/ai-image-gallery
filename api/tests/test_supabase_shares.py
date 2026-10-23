"""SupabaseShareRepo against mocked PostgREST HTTP and the real SDK.

What the SQL does (who may share, accept and see what) is covered by
supabase/tests/gallery_shares_test.sql. These check what the adapter asks PostgREST for, how it
reads the answer, and how it turns the database's refusals into domain errors.
"""

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from postgrest import APIError

from gallery.adapters.supabase_client import AsyncClient, close_client, user_client
from gallery.adapters.supabase_shares import SupabaseShareRepo
from gallery.config import Settings
from gallery.domain.errors import ShareRefusedError
from gallery.domain.models import GalleryShare, ShareDirection, ShareStatus

BASE = "http://localhost:54321/rest/v1"
FRIEND_ID = "0d4c3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a"


def share_row(**overrides: Any) -> dict[str, Any]:
    """One row as list_gallery_shares and share_gallery return it."""
    row: dict[str, Any] = {
        "id": 7,
        "direction": "outgoing",
        "other_user_id": FRIEND_ID,
        "email": "friend@example.test",
        "status": "pending",
        "created_at": "2026-10-22T09:00:00+00:00",
        "accepted_at": None,
    }
    return row | overrides


def refusal(status: int, code: str) -> httpx.Response:
    """How PostgREST answers a function that raised SQLSTATE `code`."""
    return httpx.Response(
        status, json={"code": code, "message": "refused", "details": None, "hint": None}
    )


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
def repo(client: AsyncClient, pauses: Pauses) -> SupabaseShareRepo:
    return SupabaseShareRepo(client, sleep=pauses.sleep, uniform=pauses.uniform)


# --- list ----------------------------------------------------------------------------------


async def test_list_maps_every_row_of_the_rpc(
    repo: SupabaseShareRepo, postgrest: respx.MockRouter
) -> None:
    route = postgrest.post(f"{BASE}/rpc/list_gallery_shares").mock(
        return_value=httpx.Response(
            200,
            json=[
                share_row(),
                share_row(
                    id=3,
                    direction="incoming",
                    email="owner@example.test",
                    status="accepted",
                    accepted_at="2026-10-21T10:00:00+00:00",
                ),
            ],
        )
    )

    shares = await repo.list()

    assert route.call_count == 1
    assert shares == [
        GalleryShare(
            id=7,
            direction=ShareDirection.OUTGOING,
            other_user_id=UUID(FRIEND_ID),
            email="friend@example.test",
            status=ShareStatus.PENDING,
            created_at=datetime(2026, 10, 22, 9, 0, tzinfo=UTC),
            accepted_at=None,
        ),
        GalleryShare(
            id=3,
            direction=ShareDirection.INCOMING,
            other_user_id=UUID(FRIEND_ID),
            email="owner@example.test",
            status=ShareStatus.ACCEPTED,
            created_at=datetime(2026, 10, 22, 9, 0, tzinfo=UTC),
            accepted_at=datetime(2026, 10, 21, 10, 0, tzinfo=UTC),
        ),
    ]


async def test_list_is_retried_after_a_transient_failure(
    repo: SupabaseShareRepo, postgrest: respx.MockRouter, pauses: Pauses
) -> None:
    route = postgrest.post(f"{BASE}/rpc/list_gallery_shares").mock(
        side_effect=[httpx.Response(504, text="Gateway Time-out"), httpx.Response(200, json=[])]
    )

    assert await repo.list() == []
    assert route.call_count == 2
    assert len(pauses.sleeps) == 1


# --- create --------------------------------------------------------------------------------


async def test_create_sends_the_address_and_returns_the_new_share(
    repo: SupabaseShareRepo, postgrest: respx.MockRouter
) -> None:
    route = postgrest.post(f"{BASE}/rpc/share_gallery").mock(
        return_value=httpx.Response(200, json=[share_row()])
    )

    share = await repo.create("friend@example.test")

    assert json.loads(route.calls.last.request.content) == {"p_email": "friend@example.test"}
    assert share.id == 7
    assert share.direction is ShareDirection.OUTGOING
    assert share.status is ShareStatus.PENDING


@pytest.mark.parametrize(
    ("status", "code", "reason"),
    [
        (404, "PT404", "no_account"),
        (409, "PT409", "already_shared"),
        (422, "PT422", "own_address"),
    ],
)
async def test_the_databases_refusals_become_share_refused_errors(
    repo: SupabaseShareRepo, postgrest: respx.MockRouter, status: int, code: str, reason: str
) -> None:
    postgrest.post(f"{BASE}/rpc/share_gallery").mock(return_value=refusal(status, code))

    with pytest.raises(ShareRefusedError) as raised:
        await repo.create("someone@example.test")

    assert raised.value.code == reason


async def test_a_gateways_own_404_is_not_read_as_no_account(
    repo: SupabaseShareRepo, postgrest: respx.MockRouter
) -> None:
    postgrest.post(f"{BASE}/rpc/share_gallery").mock(
        return_value=httpx.Response(404, text="<html>Not Found</html>")
    )

    with pytest.raises(APIError):
        await repo.create("friend@example.test")


async def test_create_is_never_retried(
    repo: SupabaseShareRepo, postgrest: respx.MockRouter
) -> None:
    # The first attempt may have landed, and a second would then answer "already shared".
    route = postgrest.post(f"{BASE}/rpc/share_gallery").mock(
        return_value=httpx.Response(504, text="Gateway Time-out")
    )

    with pytest.raises(APIError):
        await repo.create("friend@example.test")

    assert route.call_count == 1


# --- accept and delete ---------------------------------------------------------------------


@pytest.mark.parametrize("answer", [True, False])
async def test_accept_reports_what_the_rpc_answered(
    repo: SupabaseShareRepo, postgrest: respx.MockRouter, answer: bool
) -> None:
    route = postgrest.post(f"{BASE}/rpc/accept_gallery_share").mock(
        return_value=httpx.Response(200, json=answer)
    )

    assert await repo.accept(7) is answer
    assert json.loads(route.calls.last.request.content) == {"p_share_id": 7}


async def test_accept_is_retried_because_accepting_twice_is_harmless(
    repo: SupabaseShareRepo, postgrest: respx.MockRouter
) -> None:
    route = postgrest.post(f"{BASE}/rpc/accept_gallery_share").mock(
        side_effect=[
            httpx.Response(503, text="Service Unavailable"),
            httpx.Response(200, json=True),
        ]
    )

    assert await repo.accept(7) is True
    assert route.call_count == 2


async def test_delete_removes_the_share_by_id(
    repo: SupabaseShareRepo, postgrest: respx.MockRouter
) -> None:
    route = postgrest.delete(f"{BASE}/gallery_shares", params={"id": "eq.7"}).mock(
        return_value=httpx.Response(200, json=[{"id": 7}])
    )

    assert await repo.delete(7) is True
    assert route.call_count == 1


async def test_deleting_a_share_the_caller_cannot_see_is_false(
    repo: SupabaseShareRepo, postgrest: respx.MockRouter
) -> None:
    postgrest.delete(f"{BASE}/gallery_shares", params={"id": "eq.7"}).mock(
        return_value=httpx.Response(200, json=[])
    )

    assert await repo.delete(7) is False
