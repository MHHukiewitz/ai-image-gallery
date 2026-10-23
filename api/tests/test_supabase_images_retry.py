"""SupabaseImageRepo's retries, against mocked PostgREST HTTP and the real SDK.

The real `postgrest` client runs, so how it reports a timeout, a 503 or a gateway's HTML page is
part of what these tests exercise. Only calls that are safe to repeat are retried: reads and the
RPCs that set a value. Inserts and deletes never are.
"""

import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from postgrest import APIError

from gallery.adapters import supabase_images
from gallery.adapters.supabase_client import AsyncClient, close_client, user_client
from gallery.adapters.supabase_images import SupabaseImageRepo
from gallery.config import Settings
from gallery.domain.models import AllImages, NewImage, SimilarTo, TextQuery

BASE = "http://localhost:54321/rest/v1"
DETAILS = f"{BASE}/image_details"
IMAGES = f"{BASE}/images"
METADATA = f"{BASE}/image_metadata"
OWNER_ID = "7b9e2c4a-1d3f-4e5a-8b6c-9d0e1f2a3b4c"
SHA = "a" * 64


def detail_row(image_id: int) -> dict[str, Any]:
    folder = f"{OWNER_ID}/{image_id}"
    return {
        "id": image_id,
        "user_id": OWNER_ID,
        "filename": f"photo-{image_id}.jpg",
        "original_path": f"{folder}/original.jpg",
        "thumbnail_path": f"{folder}/thumb.webp",
        "mime_type": "image/jpeg",
        "size_bytes": 1234,
        "width": 640,
        "height": 480,
        "content_sha256": SHA,
        "uploaded_at": "2026-10-20T12:00:00+00:00",
        "description": None,
        "tags": [],
        "colors": [],
        "ai_processing_status": "pending",
        "last_error": None,
        "last_uploaded_at": "2026-10-21T08:00:00+00:00",
    }


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
        content_sha256=SHA,
        stored_sha256=SHA,
    )


# What PostgREST and the gateways in front of it answered during outages.
def schema_cache_unavailable() -> httpx.Response:
    return httpx.Response(
        503,
        json={
            "code": "PGRST002",
            "message": "Could not query the database for the schema cache. Retrying.",
            "details": None,
            "hint": None,
        },
    )


def gateway_timeout() -> httpx.Response:
    return httpx.Response(504, text="<html><body>504 Gateway Time-out</body></html>")


def rate_limited() -> httpx.Response:
    return httpx.Response(429, json={"message": "Too many requests"})


def permission_denied() -> httpx.Response:
    return httpx.Response(
        401, json={"code": "PGRST301", "message": "JWT expired", "details": None, "hint": None}
    )


def bad_request() -> httpx.Response:
    return httpx.Response(
        400, json={"code": "22P02", "message": "invalid input", "details": None, "hint": None}
    )


class Pauses:
    """Stands in for `asyncio.sleep` and `random.uniform`, and keeps what they were asked."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self.ranges: list[tuple[float, float]] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def uniform(self, low: float, high: float) -> float:
        self.ranges.append((low, high))
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


# --- a hung read retries, then succeeds ---------------------------------------------------------


async def test_a_read_that_gets_no_answer_is_retried_and_then_succeeds(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter, pauses: Pauses
) -> None:
    # The incident: the re-read after a bump hung until the client gave up.
    route = postgrest.get(DETAILS).mock(
        side_effect=[
            httpx.ReadTimeout("no answer in 8 s"),
            httpx.Response(200, json=[detail_row(7)]),
        ]
    )

    record = await repo.get(7)

    assert record is not None
    assert record.id == 7
    assert route.call_count == 2
    assert len(pauses.sleeps) == 1


async def test_a_read_is_tried_three_times_and_then_the_error_is_raised(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter, pauses: Pauses
) -> None:
    route = postgrest.get(DETAILS).mock(side_effect=httpx.ReadTimeout("no answer in 8 s"))

    with pytest.raises(httpx.ReadTimeout):
        await repo.get(7)

    assert route.call_count == 3
    assert len(pauses.sleeps) == 2


async def test_the_pauses_use_full_jitter_over_a_growing_window(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter, pauses: Pauses
) -> None:
    postgrest.get(DETAILS).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(httpx.ConnectError):
        await repo.get(7)

    # Each pause is drawn from 0 up to a ceiling that doubles.
    assert pauses.ranges == [(0, 0.2), (0, 0.4)]


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("no answer"),
        httpx.ConnectTimeout("no connection"),
        httpx.ConnectError("refused"),
        schema_cache_unavailable(),
        gateway_timeout(),
        rate_limited(),
    ],
    ids=["read-timeout", "connect-timeout", "connect-error", "503-pgrst002", "504-html", "429"],
)
async def test_transient_failures_of_a_read_are_retried(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter, failure: httpx.Response | Exception
) -> None:
    route = postgrest.get(DETAILS).mock(
        side_effect=[failure, httpx.Response(200, json=[detail_row(7)])]
    )

    assert await repo.get(7) is not None
    assert route.call_count == 2


@pytest.mark.parametrize(
    "failure", [permission_denied(), bad_request()], ids=["401-jwt-expired", "400-bad-input"]
)
async def test_errors_a_retry_cannot_fix_are_raised_at_once(
    repo: SupabaseImageRepo,
    postgrest: respx.MockRouter,
    pauses: Pauses,
    failure: httpx.Response,
) -> None:
    route = postgrest.get(DETAILS).mock(return_value=failure)

    with pytest.raises(APIError):
        await repo.get(7)

    assert route.call_count == 1
    assert pauses.sleeps == []


async def test_a_503_is_retried_by_the_repo_not_by_the_sdk(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter, pauses: Pauses
) -> None:
    # The SDK retries a GET that gets a 503 on its own, sleeping 1, 2 and 4 s. That would stack on
    # top of the repo's retry, so the repo turns it off: three attempts, not twelve.
    route = postgrest.get(DETAILS).mock(side_effect=lambda request: schema_cache_unavailable())

    with pytest.raises(APIError):
        await repo.get(7)

    assert route.call_count == 3


async def test_each_retry_is_logged_as_a_warning_with_its_operation(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    postgrest.get(DETAILS).mock(
        side_effect=[httpx.ReadTimeout("slow"), gateway_timeout(), httpx.Response(200, json=[])]
    )

    with caplog.at_level(logging.WARNING, logger=supabase_images.logger.name):
        await repo.get(7)

    first, second = caplog.records
    assert (first.levelno, first.operation, first.status, first.attempt) == (
        logging.WARNING,
        "get",
        "ReadTimeout",
        1,
    )
    assert (second.operation, second.status, second.attempt) == ("get", "504", 2)


# --- every idempotent call retries -------------------------------------------------------------


async def test_the_hash_lookup_is_retried(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter
) -> None:
    route = postgrest.get(DETAILS).mock(
        side_effect=[gateway_timeout(), httpx.Response(200, json=[detail_row(3)])]
    )

    matches = await repo.find_by_sha256(UUID(OWNER_ID), [SHA])

    assert matches[SHA].id == 3
    assert route.call_count == 2


@pytest.mark.parametrize(
    ("query", "method", "url"),
    [
        (AllImages(), "get", DETAILS),
        (TextQuery("sunset"), "post", f"{BASE}/rpc/search_images"),
        (SimilarTo(3), "post", f"{BASE}/rpc/similar_images"),
    ],
    ids=["all-images", "text-search", "similar"],
)
async def test_listing_the_gallery_is_retried(
    repo: SupabaseImageRepo,
    postgrest: respx.MockRouter,
    query: Any,
    method: str,
    url: str,
) -> None:
    route = postgrest.route(method=method.upper(), url=url).mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json=[detail_row(3)])]
    )

    page = await repo.list(query, limit=20, offset=0)

    assert [image.id for image in page.items] == [3]
    assert route.call_count == 2


async def test_requesting_a_reanalysis_is_retried(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter
) -> None:
    route = postgrest.post(f"{BASE}/rpc/request_reanalysis").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json=True)]
    )

    assert await repo.request_reanalysis(3) is True
    assert route.call_count == 2


async def test_bumping_is_retried_and_the_read_after_it_is_retried_on_its_own(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter
) -> None:
    # The incident exactly: the bump landed, then its re-read hung. Only the read runs again.
    bump = postgrest.post(f"{BASE}/rpc/bump_images").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json=[7])]
    )
    read = postgrest.get(DETAILS).mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json=[detail_row(7)])]
    )

    records = await repo.bump_many([7])

    assert [record.id for record in records] == [7]
    # The RPC timed out once and was retried. The read then timed out and ran again without
    # sending the bump a third time.
    assert bump.call_count == 2
    assert read.call_count == 2


async def test_bump_is_retried_like_bump_many(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter
) -> None:
    postgrest.post(f"{BASE}/rpc/bump_images").mock(
        side_effect=[gateway_timeout(), httpx.Response(200, json=[7])]
    )
    postgrest.get(DETAILS).mock(return_value=httpx.Response(200, json=[detail_row(7)]))

    record = await repo.bump(7)

    assert record is not None
    assert record.id == 7


# --- writes that may have landed are never retried ---------------------------------------------


async def test_an_insert_that_times_out_is_not_retried(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter, pauses: Pauses
) -> None:
    # The first attempt may have inserted the row; a second would add a duplicate.
    route = postgrest.post(IMAGES).mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(httpx.ReadTimeout):
        await repo.insert(new_image())

    assert route.call_count == 1
    assert pauses.sleeps == []


async def test_an_insert_that_gets_a_503_is_not_retried(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter, pauses: Pauses
) -> None:
    route = postgrest.post(IMAGES).mock(return_value=schema_cache_unavailable())

    with pytest.raises(APIError):
        await repo.insert(new_image())

    assert route.call_count == 1
    assert pauses.sleeps == []


async def test_a_failed_metadata_insert_is_not_retried_and_cleans_up_the_image_row(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter, pauses: Pauses
) -> None:
    postgrest.post(IMAGES).mock(return_value=httpx.Response(201, json=[{"id": 11}]))
    metadata = postgrest.post(METADATA).mock(side_effect=httpx.ReadTimeout("slow"))
    cleanup = postgrest.delete(IMAGES).mock(return_value=httpx.Response(200, json=[{"id": 11}]))

    with pytest.raises(httpx.ReadTimeout):
        await repo.insert(new_image())

    assert metadata.call_count == 1
    assert cleanup.call_count == 1
    assert pauses.sleeps == []


async def test_the_read_after_an_insert_is_retried(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter
) -> None:
    postgrest.post(IMAGES).mock(return_value=httpx.Response(201, json=[{"id": 11}]))
    postgrest.post(METADATA).mock(return_value=httpx.Response(201, json=[]))
    read = postgrest.get(DETAILS).mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json=[detail_row(11)])]
    )

    record = await repo.insert(new_image())

    assert record.id == 11
    assert read.call_count == 2


async def test_a_delete_that_times_out_is_not_retried(
    repo: SupabaseImageRepo, postgrest: respx.MockRouter, pauses: Pauses
) -> None:
    postgrest.get(DETAILS).mock(return_value=httpx.Response(200, json=[detail_row(7)]))
    delete = postgrest.delete(IMAGES).mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(httpx.ReadTimeout):
        await repo.delete(7)

    assert delete.call_count == 1
    assert pauses.sleeps == []


# --- the defaults ------------------------------------------------------------------------------


async def test_a_repo_built_without_hooks_retries_with_the_real_clock(
    client: AsyncClient, postgrest: respx.MockRouter
) -> None:
    route = postgrest.get(DETAILS).mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json=[detail_row(7)])]
    )

    # The first pause is at most 0.2 s.
    assert await SupabaseImageRepo(client).get(7) is not None
    assert route.call_count == 2
