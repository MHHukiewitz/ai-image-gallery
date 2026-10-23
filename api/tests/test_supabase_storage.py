"""SupabaseObjectStore against mocked Storage HTTP: no network and no Supabase stack.

The real `storage3` client runs, so its error parsing is part of what these tests exercise.
"""

import json
import logging
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from io import BytesIO
from uuid import UUID

import httpx
import pytest
import respx
from PIL import Image
from storage3.exceptions import StorageApiError

from gallery.adapters import supabase_storage
from gallery.adapters.supabase_client import AsyncClient, close_client, user_client
from gallery.adapters.supabase_storage import SignedUrlCache, SupabaseObjectStore
from gallery.config import Settings
from gallery.domain.errors import QuotaExceededError
from gallery.domain.models import ImageRecord, NewImage
from gallery.services.uploads import UploadService

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


def storage_quota_refusal() -> httpx.Response:
    """What storage-api v1.82 answers when enforce_storage_quota raises PT429 (seen on the local
    stack): a 500 that names the SQLSTATE and drops the database's message."""
    return httpx.Response(
        500,
        json={
            "statusCode": "500",
            "error": "DatabaseError",
            "message": "database error, code: PT429",
            "code": "DatabaseError",
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


@pytest.fixture(autouse=True)
def fresh_shared_cache(monkeypatch: pytest.MonkeyPatch) -> SignedUrlCache:
    """Give each test its own process-wide cache, so signed URLs don't leak between tests."""
    cache = SignedUrlCache()
    monkeypatch.setattr(supabase_storage, "_signed_urls", cache)
    return cache


class Clock:
    """A clock the test winds forward by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


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


# --- the storage quota ---------------------------------------------------------------------


async def test_a_storage_quota_refusal_is_a_quota_error_with_a_short_retry_after(
    store: SupabaseObjectStore, storage_http: respx.MockRouter, pauses: Pauses
) -> None:
    upload = storage_http.post(UPLOAD_URL).mock(return_value=storage_quota_refusal())

    with pytest.raises(QuotaExceededError) as excinfo:
        await store.put(PATH, b"data", "image/webp")

    assert excinfo.value.message == (
        "You've reached your storage limit. Delete some images to make room."
    )
    # A slot frees as soon as the user deletes an image, so there is no day to wait out.
    assert excinfo.value.retry_after_seconds == 60
    # It's a 500, but retrying can't free a slot.
    assert upload.call_count == 1
    assert pauses.sleeps == []


async def test_a_429_that_carries_the_pt429_code_is_a_quota_error_too(
    store: SupabaseObjectStore, storage_http: respx.MockRouter, pauses: Pauses
) -> None:
    """How a storage-api that answers PT429 the way PostgREST does would send it."""
    body = {"statusCode": "429", "error": "PT429", "message": "You've reached your storage limit"}
    upload = storage_http.post(UPLOAD_URL).mock(return_value=httpx.Response(429, json=body))

    with pytest.raises(QuotaExceededError):
        await store.put(PATH, b"data", "image/webp")

    assert upload.call_count == 1


async def test_another_database_error_is_still_retried(
    store: SupabaseObjectStore, storage_http: respx.MockRouter, pauses: Pauses
) -> None:
    body = {"statusCode": "500", "error": "DatabaseError", "message": "database error, code: XX000"}
    upload = storage_http.post(UPLOAD_URL).mock(
        side_effect=lambda request: httpx.Response(500, json=body)
    )

    with pytest.raises(StorageApiError):
        await store.put(PATH, b"data", "image/webp")

    assert upload.call_count == 4


async def test_a_refusal_after_a_transient_failure_is_a_quota_error(
    store: SupabaseObjectStore, storage_http: respx.MockRouter
) -> None:
    storage_http.post(UPLOAD_URL).mock(
        side_effect=[too_many_connections(), storage_quota_refusal()]
    )

    with pytest.raises(QuotaExceededError):
        await store.put(PATH, b"data", "image/webp")


async def test_a_storage_quota_refusal_is_logged_as_a_user_mistake(
    store: SupabaseObjectStore, storage_http: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    storage_http.post(UPLOAD_URL).mock(return_value=storage_quota_refusal())

    with (
        caplog.at_level(logging.INFO, logger=supabase_storage.__name__),
        pytest.raises(QuotaExceededError),
    ):
        await store.put(PATH, b"data", "image/webp")

    # INFO, not ERROR: Sentry gets ERROR and above, and this is the user's limit, not a bug.
    assert [record.levelno for record in caplog.records] == [logging.INFO]


# --- signed-url cache ----------------------------------------------------------------------


def sign_response(*paths: str) -> httpx.Response:
    return httpx.Response(200, json=[signed_entry(path) for path in paths])


def signed_paths(route: respx.Route, call: int) -> list[str]:
    return json.loads(route.calls[call].request.content)["paths"]


async def test_a_second_call_is_served_from_the_cache(
    store: SupabaseObjectStore, storage_http: respx.MockRouter
) -> None:
    sign = storage_http.post(SIGN_URL).mock(return_value=sign_response(PATH))

    first = await store.signed_urls([PATH], 3600)
    second = await store.signed_urls([PATH], 3600)

    assert first == second == {PATH: f"{BASE}/object/sign/images/{PATH}?token=t"}
    assert sign.call_count == 1


async def test_the_cache_is_shared_by_separately_built_stores(
    client: AsyncClient, storage_http: respx.MockRouter
) -> None:
    sign = storage_http.post(SIGN_URL).mock(return_value=sign_response(PATH))

    await SupabaseObjectStore(client, "images").signed_urls([PATH], 3600)
    await SupabaseObjectStore(client, "images").signed_urls([PATH], 3600)

    assert sign.call_count == 1


async def test_only_the_cache_misses_are_signed_and_in_one_call(
    client: AsyncClient, storage_http: respx.MockRouter
) -> None:
    store = SupabaseObjectStore(client, "images", cache=SignedUrlCache())
    sign = storage_http.post(SIGN_URL).mock(
        side_effect=[sign_response("a"), sign_response("b", "c")]
    )
    await store.signed_urls(["a"], 3600)

    urls = await store.signed_urls(["a", "b", "c"], 3600)

    assert sign.call_count == 2
    assert signed_paths(sign, 1) == ["b", "c"]
    assert set(urls) == {"a", "b", "c"}


async def test_a_fully_cached_page_makes_no_request(
    client: AsyncClient, storage_http: respx.MockRouter
) -> None:
    store = SupabaseObjectStore(client, "images", cache=SignedUrlCache())
    sign = storage_http.post(SIGN_URL).mock(return_value=sign_response("a", "b"))
    await store.signed_urls(["a", "b"], 3600)

    await store.signed_urls(["b", "a"], 3600)

    assert sign.call_count == 1


async def test_a_path_asked_for_twice_is_signed_once(
    client: AsyncClient, storage_http: respx.MockRouter
) -> None:
    store = SupabaseObjectStore(client, "images", cache=SignedUrlCache())
    sign = storage_http.post(SIGN_URL).mock(return_value=sign_response("a"))

    await store.signed_urls(["a", "a"], 3600)

    assert signed_paths(sign, 0) == ["a"]


async def test_an_entry_close_to_expiry_is_signed_again(
    client: AsyncClient, storage_http: respx.MockRouter
) -> None:
    clock = Clock()
    store = SupabaseObjectStore(client, "images", cache=SignedUrlCache(clock=clock))
    sign = storage_http.post(SIGN_URL).mock(return_value=sign_response("a"))
    await store.signed_urls(["a"], 3600)

    clock.now += 3600 - 11 * 60  # 11 minutes of validity left: still reused
    await store.signed_urls(["a"], 3600)
    assert sign.call_count == 1

    clock.now += 2 * 60  # 9 minutes left: too close to expiry
    await store.signed_urls(["a"], 3600)
    assert sign.call_count == 2


async def test_a_refreshed_entry_is_reused_again(
    client: AsyncClient, storage_http: respx.MockRouter
) -> None:
    clock = Clock()
    store = SupabaseObjectStore(client, "images", cache=SignedUrlCache(clock=clock))
    sign = storage_http.post(SIGN_URL).mock(return_value=sign_response("a"))
    await store.signed_urls(["a"], 3600)
    clock.now += 3600
    await store.signed_urls(["a"], 3600)

    await store.signed_urls(["a"], 3600)

    assert sign.call_count == 2


async def test_the_ttl_and_the_bucket_are_part_of_the_key(
    client: AsyncClient, storage_http: respx.MockRouter
) -> None:
    cache = SignedUrlCache()
    sign = storage_http.post(SIGN_URL).mock(return_value=sign_response("a"))
    other_sign = storage_http.post(f"{BASE}/object/sign/other").mock(
        return_value=sign_response("a")
    )

    await SupabaseObjectStore(client, "images", cache=cache).signed_urls(["a"], 3600)
    await SupabaseObjectStore(client, "images", cache=cache).signed_urls(["a"], 7200)
    await SupabaseObjectStore(client, "other", cache=cache).signed_urls(["a"], 3600)

    assert (sign.call_count, other_sign.call_count) == (2, 1)


async def test_a_path_that_could_not_be_signed_is_not_cached(
    client: AsyncClient, storage_http: respx.MockRouter
) -> None:
    store = SupabaseObjectStore(client, "images", cache=SignedUrlCache())
    missing = {"path": "gone", "signedURL": None, "error": "Object not found"}
    sign = storage_http.post(SIGN_URL).mock(
        return_value=httpx.Response(200, json=[missing, signed_entry("a")])
    )

    urls = await store.signed_urls(["gone", "a"], 3600)
    await store.signed_urls(["gone", "a"], 3600)

    assert set(urls) == {"a"}
    assert signed_paths(sign, 1) == ["gone"]


async def test_a_failed_signing_call_caches_nothing(
    client: AsyncClient, storage_http: respx.MockRouter
) -> None:
    cache = SignedUrlCache()
    store = SupabaseObjectStore(client, "images", cache=cache)
    storage_http.post(SIGN_URL).mock(side_effect=lambda request: storage_error(400))

    with pytest.raises(StorageApiError):
        await store.signed_urls(["a"], 3600)

    assert len(cache) == 0


async def test_the_cache_keeps_at_most_its_bound_and_drops_the_least_recently_used(
    client: AsyncClient, storage_http: respx.MockRouter
) -> None:
    cache = SignedUrlCache(max_entries=3)
    store = SupabaseObjectStore(client, "images", cache=cache)
    sign = storage_http.post(SIGN_URL).mock(
        side_effect=lambda request: sign_response(*json.loads(request.content)["paths"])
    )
    await store.signed_urls(["a", "b", "c"], 3600)
    await store.signed_urls(["a"], 3600)  # a is now the most recently used

    await store.signed_urls(["d"], 3600)  # pushes out b, the least recently used

    assert len(cache) == 3
    sign.reset()
    await store.signed_urls(["a", "c", "d"], 3600)
    assert sign.call_count == 0
    await store.signed_urls(["b"], 3600)
    assert sign.call_count == 1


def test_the_default_bound_is_five_thousand_entries() -> None:
    cache = SignedUrlCache()
    for index in range(5_001):
        cache.put(("images", f"path-{index}", 3600), "url", 3600)

    assert len(cache) == 5_000
    assert cache.get(("images", "path-0", 3600)) is None
    assert cache.get(("images", "path-1", 3600)) == "url"


def test_threads_can_share_the_cache() -> None:
    cache = SignedUrlCache(max_entries=50)
    failures: list[BaseException] = []

    def hammer(offset: int) -> None:
        try:
            for index in range(2_000):
                key = ("images", f"path-{(index + offset) % 80}", 3600)
                cache.put(key, "url", 3600)
                cache.get(key)
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=hammer, args=(offset,)) for offset in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert len(cache) <= 50


# --- a failed upload leaves nothing behind -------------------------------------------------


class UnreachableImageRepo:
    """An ImageRepo that must not be reached: the upload dies before the insert."""

    async def find_by_sha256(self, user_id: UUID, sha256s: Sequence[str]) -> dict[str, ImageRecord]:
        return {}

    async def insert(self, image: NewImage) -> ImageRecord:
        raise AssertionError("the thumbnail failed, so nothing should be inserted")


async def test_a_failed_thumbnail_put_deletes_the_original(
    store: SupabaseObjectStore, storage_http: respx.MockRouter
) -> None:
    owner = UUID("7b9e2c4a-1d3f-4e5a-8b6c-9d0e1f2a3b4c")
    original = storage_http.post(url__regex=rf"{BASE}/object/images/{owner}/[^/]+/original\.png")
    thumbnail = storage_http.post(url__regex=rf"{BASE}/object/images/{owner}/[^/]+/thumb\.webp")
    remove = storage_http.delete(REMOVE_URL)
    original.mock(return_value=httpx.Response(200, json={"Key": "original"}))
    thumbnail.mock(side_effect=lambda request: too_many_connections())
    remove.mock(return_value=httpx.Response(200, json=[]))
    png = BytesIO()
    Image.new("RGB", (64, 48), "teal").save(png, format="PNG")
    service = UploadService(
        UnreachableImageRepo(),
        store,
        max_bytes=1024 * 1024,
        max_pixels=1_000_000,
        signed_url_ttl=3600,
    )

    with pytest.raises(StorageApiError):
        await service.upload(owner, "beach.png", png.getvalue())

    assert original.call_count == 1
    assert thumbnail.call_count == 4  # retried before giving up
    [removal] = remove.calls
    removed = json.loads(removal.request.content)["prefixes"]
    assert any(path.endswith("/original.png") for path in removed)


async def test_a_thumbnail_refused_by_the_storage_quota_deletes_the_original(
    store: SupabaseObjectStore, storage_http: respx.MockRouter
) -> None:
    """The original took the last free slot, so the thumbnail is refused, and the original must
    go too: an image without its thumbnail would hold a slot and show nothing."""
    owner = UUID("7b9e2c4a-1d3f-4e5a-8b6c-9d0e1f2a3b4c")
    original = storage_http.post(url__regex=rf"{BASE}/object/images/{owner}/[^/]+/original\.png")
    thumbnail = storage_http.post(url__regex=rf"{BASE}/object/images/{owner}/[^/]+/thumb\.webp")
    remove = storage_http.delete(REMOVE_URL)
    original.mock(return_value=httpx.Response(200, json={"Key": "original"}))
    thumbnail.mock(return_value=storage_quota_refusal())
    remove.mock(return_value=httpx.Response(200, json=[]))
    png = BytesIO()
    Image.new("RGB", (64, 48), "teal").save(png, format="PNG")
    service = UploadService(
        UnreachableImageRepo(),
        store,
        max_bytes=1024 * 1024,
        max_pixels=1_000_000,
        signed_url_ttl=3600,
    )

    with pytest.raises(QuotaExceededError):
        await service.upload(owner, "beach.png", png.getvalue())

    assert thumbnail.call_count == 1
    [removal] = remove.calls
    removed = json.loads(removal.request.content)["prefixes"]
    assert any(path.endswith("/original.png") for path in removed)
