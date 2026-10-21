"""SupabaseImageRepo.count_uploaded_since, against mocked PostgREST HTTP and the real SDK.

The count is the whole input to the daily quota, so these check the request that goes out (a HEAD
on `upload_log` with `count=exact`, filtered on `uploaded_at`) and how the answer is read back from
Content-Range. The log keeps a row after its image is deleted, which `images` would not.
A count is a read, so a transient failure is retried.
"""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import httpx
import pytest
import respx

from gallery.adapters.supabase_client import AsyncClient, close_client, user_client
from gallery.adapters.supabase_images import SupabaseImageRepo
from gallery.config import Settings

UPLOAD_LOG = "http://localhost:54321/rest/v1/upload_log"
SINCE = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


class Pauses:
    """Stands in for `asyncio.sleep` and `random.uniform`, and keeps what they were asked."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def uniform(self, low: float, high: float) -> float:
        return high


@pytest.fixture
def pauses() -> Pauses:
    return Pauses()


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
def repo(client: AsyncClient, pauses: Pauses) -> SupabaseImageRepo:
    return SupabaseImageRepo(client, sleep=pauses.sleep, uniform=pauses.uniform)


def counted(content_range: str) -> httpx.Response:
    """PostgREST's answer to a HEAD with count=exact: no body, the count in Content-Range."""
    return httpx.Response(200, headers={"Content-Range": content_range})


async def test_counts_the_uploads_since_the_given_time_with_one_head_request(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter
) -> None:
    route = postgrest.head(UPLOAD_LOG).mock(return_value=counted("0-0/42"))

    assert await repo.count_uploaded_since(SINCE) == 42

    [call] = route.calls
    request = call.request
    assert request.method == "HEAD"
    assert request.url.params["select"] == "id"
    assert request.url.params["uploaded_at"] == "gte.2026-10-19T12:00:00+00:00"
    assert "count=exact" in request.headers["Prefer"]


async def test_no_uploads_in_the_window_counts_as_zero(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter
) -> None:
    postgrest.head(UPLOAD_LOG).mock(return_value=counted("*/0"))

    assert await repo.count_uploaded_since(SINCE) == 0


async def test_an_answer_without_a_count_counts_as_zero(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter
) -> None:
    postgrest.head(UPLOAD_LOG).mock(return_value=httpx.Response(200))

    assert await repo.count_uploaded_since(SINCE) == 0


async def test_a_count_that_fails_once_is_retried_by_the_repo_and_then_succeeds(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter, pauses: Pauses
) -> None:
    route = postgrest.head(UPLOAD_LOG).mock(
        side_effect=[
            httpx.Response(503, json={"code": "PGRST002", "message": "Retrying."}),
            counted("0-0/7"),
        ]
    )

    assert await repo.count_uploaded_since(SINCE) == 7
    assert route.call_count == 2
    assert len(pauses.sleeps) == 1
