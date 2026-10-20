"""SupabaseObjectStore against mocked Storage HTTP: no network and no Supabase stack.

The real `storage3` client runs, so its error parsing is part of what these tests exercise.
"""

import logging
from collections.abc import AsyncIterator, Callable, Iterator

import httpx
import pytest
import respx
from storage3.exceptions import StorageApiError

from gallery.adapters import supabase_storage
from gallery.adapters.supabase_client import AsyncClient, close_client, user_client
from gallery.adapters.supabase_storage import SupabaseObjectStore
from gallery.config import Settings

BASE = "http://localhost:54321/storage/v1"
PATH = "7b9e2c4a-1d3f-4e5a-8b6c-9d0e1f2a3b4c/1/thumb.webp"
UPLOAD_URL = f"{BASE}/object/images/{PATH}"
DOWNLOAD_URL = f"{BASE}/object/images/{PATH}"
REMOVE_URL = f"{BASE}/object/images"
SIGN_URL = f"{BASE}/object/sign/images"


def too_many_connections(status_code: object = "429") -> httpx.Response:
    """What Supabase Storage answered during the incident. It sends `statusCode` as a string."""
    return httpx.Response(
        429,
        json={
            "statusCode": status_code,
            "error": "too_many_connections",
            "message": "Too many connections issued to the database",
        },
    )


def storage_error(status_code: int, error: str = "Internal") -> httpx.Response:
    body = {"statusCode": str(status_code), "error": error, "message": "nope"}
    return httpx.Response(status_code, json=body)


class Pauses:
    """Stands in for `asyncio.sleep` and `random.uniform`, and keeps what they were asked."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self.ranges: list[tuple[float, float]] = []
        self.draw: Callable[[float, float], float] = lambda low, high: high

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def uniform(self, low: float, high: float) -> float:
        self.ranges.append((low, high))
        return self.draw(low, high)


@pytest.fixture
def pauses() -> Pauses:
    return Pauses()


@pytest.fixture
def storage_http() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    client = await user_client(settings, "test-token")
    yield client
    await close_client(client)


@pytest.fixture
def store(client: AsyncClient, pauses: Pauses) -> SupabaseObjectStore:
    return SupabaseObjectStore(client, "images", sleep=pauses.sleep, uniform=pauses.uniform)


def signed_entry(path: str) -> dict[str, object]:
    return {"path": path, "signedURL": f"/object/sign/images/{path}?token=t", "error": None}


# --- retries -------------------------------------------------------------------------------


async def test_a_429_followed_by_success_returns_after_two_attempts(
    store: SupabaseObjectStore, storage_http: respx.MockRouter, pauses: Pauses
) -> None:
    upload = storage_http.post(UPLOAD_URL).mock(
        side_effect=[too_many_connections(), httpx.Response(200, json={"Key": PATH})]
    )

    await store.put(PATH, b"data", "image/webp")

    assert upload.call_count == 2
    assert len(pauses.sleeps) == 1


async def test_a_persistent_429_raises_after_four_attempts(
    store: SupabaseObjectStore, storage_http: respx.MockRouter, pauses: Pauses
) -> None:
    upload = storage_http.post(UPLOAD_URL).mock(side_effect=lambda request: too_many_connections())

    with pytest.raises(StorageApiError) as excinfo:
        await store.put(PATH, b"data", "image/webp")

    assert upload.call_count == 4
    assert excinfo.value.code == "too_many_connections"
    assert len(pauses.sleeps) == 3


async def test_a_400_is_not_retried(
    store: SupabaseObjectStore, storage_http: respx.MockRouter, pauses: Pauses
) -> None:
    upload = storage_http.post(UPLOAD_URL).mock(return_value=storage_error(400, "InvalidRequest"))

    with pytest.raises(StorageApiError):
        await store.put(PATH, b"data", "image/webp")

    assert upload.call_count == 1
    assert pauses.sleeps == []


@pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
async def test_every_transient_status_is_retried(
    store: SupabaseObjectStore, storage_http: respx.MockRouter, status_code: int
) -> None:
    download = storage_http.get(DOWNLOAD_URL).mock(
        side_effect=[storage_error(status_code), httpx.Response(200, content=b"bytes")]
    )

    assert await store.get(PATH) == b"bytes"
    assert download.call_count == 2


@pytest.mark.parametrize("status_code", [401, 403, 404, 409, 413])
async def test_other_client_errors_are_not_retried(
    store: SupabaseObjectStore, storage_http: respx.MockRouter, status_code: int
) -> None:
    download = storage_http.get(DOWNLOAD_URL).mock(return_value=storage_error(status_code))

    with pytest.raises(StorageApiError):
        await store.get(PATH)

    assert download.call_count == 1


async def test_a_status_sent_as_a_number_is_recognised_too(
    store: SupabaseObjectStore, storage_http: respx.MockRouter
) -> None:
    download = storage_http.get(DOWNLOAD_URL).mock(
        side_effect=[too_many_connections(status_code=429), httpx.Response(200, content=b"ok")]
    )

    assert await store.get(PATH) == b"ok"
    assert download.call_count == 2


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("slow"),
        httpx.ReadError("reset"),
        httpx.ReadTimeout("slow"),
    ],
    ids=lambda failure: type(failure).__name__,
)
async def test_network_errors_are_retried(
    store: SupabaseObjectStore, storage_http: respx.MockRouter, failure: Exception
) -> None:
    download = storage_http.get(DOWNLOAD_URL).mock(
        side_effect=[failure, httpx.Response(200, content=b"bytes")]
    )

    assert await store.get(PATH) == b"bytes"
    assert download.call_count == 2


async def test_a_persistent_network_error_surfaces_the_original_error(
    store: SupabaseObjectStore, storage_http: respx.MockRouter
) -> None:
    download = storage_http.get(DOWNLOAD_URL).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(httpx.ConnectError):
        await store.get(PATH)

    assert download.call_count == 4


async def test_an_error_that_is_not_a_storage_or_network_error_is_not_retried(
    store: SupabaseObjectStore, storage_http: respx.MockRouter
) -> None:
    download = storage_http.get(DOWNLOAD_URL).mock(side_effect=ValueError("a bug"))

    with pytest.raises(ValueError, match="a bug"):
        await store.get(PATH)

    assert download.call_count == 1


async def test_delete_and_signed_urls_are_retried_too(
    store: SupabaseObjectStore, storage_http: respx.MockRouter
) -> None:
    remove = storage_http.delete(REMOVE_URL).mock(
        side_effect=[too_many_connections(), httpx.Response(200, json=[])]
    )
    sign = storage_http.post(SIGN_URL).mock(
        side_effect=[storage_error(503), httpx.Response(200, json=[signed_entry(PATH)])]
    )

    await store.delete([PATH])
    urls = await store.signed_urls([PATH], 60)

    assert remove.call_count == 2
    assert sign.call_count == 2
    assert urls == {PATH: f"{BASE}/object/sign/images/{PATH}?token=t"}


async def test_deleting_nothing_makes_no_request(
    store: SupabaseObjectStore, storage_http: respx.MockRouter
) -> None:
    remove = storage_http.delete(REMOVE_URL)

    await store.delete([])

    assert remove.call_count == 0


# --- backoff and jitter --------------------------------------------------------------------


async def test_each_wait_is_drawn_from_zero_to_a_doubling_ceiling(
    store: SupabaseObjectStore, storage_http: respx.MockRouter, pauses: Pauses
) -> None:
    storage_http.get(DOWNLOAD_URL).mock(side_effect=lambda request: too_many_connections())

    with pytest.raises(StorageApiError):
        await store.get(PATH)

    assert pauses.ranges == [(0, 0.2), (0, 0.4), (0, 0.8)]


async def test_the_wait_is_the_drawn_value(
    store: SupabaseObjectStore, storage_http: respx.MockRouter, pauses: Pauses
) -> None:
    pauses.draw = lambda low, high: (low + high) / 4
    storage_http.get(DOWNLOAD_URL).mock(side_effect=lambda request: too_many_connections())

    with pytest.raises(StorageApiError):
        await store.get(PATH)

    assert pauses.sleeps == pytest.approx([0.05, 0.1, 0.2])


async def test_the_ceiling_stops_doubling_at_two_seconds(
    store: SupabaseObjectStore,
    storage_http: respx.MockRouter,
    pauses: Pauses,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supabase_storage, "_MAX_ATTEMPTS", 8)
    storage_http.get(DOWNLOAD_URL).mock(side_effect=lambda request: too_many_connections())

    with pytest.raises(StorageApiError):
        await store.get(PATH)

    ceilings = [high for _, high in pauses.ranges]
    assert ceilings == pytest.approx([0.2, 0.4, 0.8, 1.6, 2.0, 2.0, 2.0])


async def test_the_default_jitter_stays_within_its_bounds(
    client: AsyncClient, storage_http: respx.MockRouter, pauses: Pauses
) -> None:
    drawn: list[float] = []

    async def record_sleep(seconds: float) -> None:
        drawn.append(seconds)

    store = SupabaseObjectStore(client, "images", sleep=record_sleep)
    storage_http.get(DOWNLOAD_URL).mock(side_effect=lambda request: too_many_connections())

    for _ in range(25):
        drawn.clear()
        with pytest.raises(StorageApiError):
            await store.get(PATH)
        assert len(drawn) == 3
        assert all(
            0 <= wait <= ceiling for wait, ceiling in zip(drawn, [0.2, 0.4, 0.8], strict=True)
        )


async def test_each_retry_is_logged_as_a_warning_with_its_operation_and_status(
    store: SupabaseObjectStore,
    storage_http: respx.MockRouter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage_http.get(DOWNLOAD_URL).mock(
        side_effect=[too_many_connections(), httpx.ReadTimeout("slow"), httpx.Response(200)]
    )

    with caplog.at_level(logging.WARNING, logger=supabase_storage.logger.name):
        await store.get(PATH)

    first, second = caplog.records
    assert (first.levelno, first.operation, first.status, first.attempt) == (
        logging.WARNING,
        "get",
        "429",
        1,
    )
    assert (second.operation, second.status, second.attempt) == ("get", "ReadTimeout", 2)


# --- put -----------------------------------------------------------------------------------


async def test_a_duplicate_on_the_first_attempt_is_an_error(
    store: SupabaseObjectStore, storage_http: respx.MockRouter
) -> None:
    storage_http.post(UPLOAD_URL).mock(return_value=storage_error(409, "Duplicate"))

    with pytest.raises(StorageApiError):
        await store.put(PATH, b"data", "image/webp")


async def test_a_duplicate_on_a_retry_means_the_first_try_landed(
    store: SupabaseObjectStore, storage_http: respx.MockRouter
) -> None:
    upload = storage_http.post(UPLOAD_URL).mock(
        side_effect=[httpx.ReadTimeout("slow"), storage_error(409, "Duplicate")]
    )

    await store.put(PATH, b"data", "image/webp")

    assert upload.call_count == 2
