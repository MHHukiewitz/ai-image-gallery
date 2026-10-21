"""The daily quota in SupabaseImageRepo, against mocked PostgREST HTTP and the real SDK.

`count_uploaded_since` is the whole input to the API's quota check, so these check the request that
goes out (a HEAD on `upload_log` with `count=exact`, filtered on `uploaded_at`) and how the answer
is read back from Content-Range. The log keeps a row after its image is deleted, which `images`
would not. A count is a read, so a transient failure is retried.

`insert` is the other half: the database refuses an image past the daily quota (#95) with the
SQLSTATE PT429, which PostgREST answers as HTTP 429, and the repo turns that into the domain's
QuotaExceededError.
"""

import logging
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
import respx
from postgrest import APIError

from gallery.adapters.supabase_client import AsyncClient, close_client, user_client
from gallery.adapters.supabase_images import SupabaseImageRepo
from gallery.config import Settings
from gallery.domain.errors import QuotaExceededError
from gallery.domain.models import NewImage

BASE = "http://localhost:54321/rest/v1"
UPLOAD_LOG = f"{BASE}/upload_log"
IMAGES = f"{BASE}/images"
METADATA = f"{BASE}/image_metadata"
IMAGE_DETAILS = f"{BASE}/image_details"
SINCE = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
OWNER_ID = "7b9e2c4a-1d3f-4e5a-8b6c-9d0e1f2a3b4c"


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


# --- the database's refusal of an insert past the quota ---------------------------------------


def new_image() -> NewImage:
    folder = f"{OWNER_ID}/1"
    return NewImage(
        user_id=UUID(OWNER_ID),
        filename="photo.jpg",
        original_path=f"{folder}/original.jpg",
        thumbnail_path=f"{folder}/thumb.webp",
        mime_type="image/jpeg",
        size_bytes=1234,
        width=640,
        height=480,
        content_sha256="a" * 64,
        stored_sha256="a" * 64,
    )


def quota_refused() -> httpx.Response:
    """What PostgREST answers when enforce_upload_quota raises SQLSTATE PT429."""
    return httpx.Response(
        429,
        json={
            "code": "PT429",
            "message": "You've reached today's upload limit of 300 images. Try again tomorrow.",
            "details": None,
            "hint": "The limit counts uploads in the last 24 hours, deleted images included.",
        },
    )


async def test_an_insert_the_database_refuses_for_the_quota_raises_quota_exceeded(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter, pauses: Pauses
) -> None:
    images = postgrest.post(IMAGES).mock(return_value=quota_refused())
    metadata = postgrest.post(METADATA).mock(return_value=httpx.Response(201, json=[]))
    cleanup = postgrest.delete(IMAGES).mock(return_value=httpx.Response(200, json=[]))

    with pytest.raises(QuotaExceededError) as refused:
        await repo.insert(new_image())

    # The database's wording is the user's message, and the longest wait is the 24-hour window.
    assert refused.value.message == (
        "You've reached today's upload limit of 300 images. Try again tomorrow."
    )
    assert refused.value.retry_after_seconds == 86400
    assert isinstance(refused.value.__cause__, APIError)
    # One attempt, no metadata for an image that does not exist, and nothing to clean up.
    assert images.call_count == 1
    assert metadata.call_count == 0
    assert cleanup.call_count == 0
    assert pauses.sleeps == []


async def test_a_quota_refusal_is_logged_at_info_not_as_an_error(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    postgrest.post(IMAGES).mock(return_value=quota_refused())

    with caplog.at_level(logging.INFO), pytest.raises(QuotaExceededError):
        await repo.insert(new_image())

    [record] = [r for r in caplog.records if r.name == "gallery.adapters.supabase_images"]
    assert record.levelno == logging.INFO


async def test_a_refusal_with_an_empty_message_still_says_the_quota_is_used_up(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter
) -> None:
    postgrest.post(IMAGES).mock(
        return_value=httpx.Response(
            429, json={"code": "PT429", "message": "", "details": None, "hint": None}
        )
    )

    with pytest.raises(QuotaExceededError) as refused:
        await repo.insert(new_image())

    assert "upload limit" in refused.value.message


async def test_a_429_that_is_not_the_quota_stays_an_api_error(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter
) -> None:
    # Supabase's own rate limit answers 429 with no PostgREST code. It must not read as "come
    # back tomorrow".
    route = postgrest.post(IMAGES).mock(
        return_value=httpx.Response(429, json={"message": "Too many requests"})
    )

    with pytest.raises(APIError):
        await repo.insert(new_image())

    assert route.call_count == 1


async def test_another_insert_error_stays_an_api_error(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter
) -> None:
    postgrest.post(IMAGES).mock(
        return_value=httpx.Response(
            403,
            json={
                "code": "42501",
                "message": 'new row violates row-level security policy for table "images"',
                "details": None,
                "hint": None,
            },
        )
    )

    with pytest.raises(APIError) as failed:
        await repo.insert(new_image())

    assert failed.value.code == "42501"


async def test_an_insert_under_the_quota_is_unchanged(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter
) -> None:
    postgrest.post(IMAGES).mock(return_value=httpx.Response(201, json=[{"id": 11}]))
    postgrest.post(METADATA).mock(return_value=httpx.Response(201, json=[]))
    postgrest.get(IMAGE_DETAILS).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 11,
                    "user_id": OWNER_ID,
                    "filename": "photo.jpg",
                    "original_path": f"{OWNER_ID}/1/original.jpg",
                    "thumbnail_path": f"{OWNER_ID}/1/thumb.webp",
                    "mime_type": "image/jpeg",
                    "size_bytes": 1234,
                    "width": 640,
                    "height": 480,
                    "content_sha256": "a" * 64,
                    "uploaded_at": "2026-10-20T12:00:00+00:00",
                    "description": None,
                    "tags": [],
                    "colors": [],
                    "ai_processing_status": "pending",
                    "last_error": None,
                    "last_uploaded_at": "2026-10-20T12:00:00+00:00",
                }
            ],
        )
    )

    record = await repo.insert(new_image())

    assert record.id == 11
