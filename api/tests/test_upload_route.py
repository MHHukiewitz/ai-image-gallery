import hashlib
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from io import BytesIO
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from gallery.api import deps
from gallery.api.deps import CurrentUser
from gallery.api.routes import uploads
from gallery.config import Settings
from gallery.domain.models import AnalysisStatus, ImageRecord, NewImage

USER_ID = UUID("7b9e2c4a-1d3f-4e5a-8b6c-9d0e1f2a3b4c")
URL = "/api/v1/images"
MEGABYTE = 1024 * 1024


class InMemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_signing = False
        self.signing_calls: list[list[str]] = []

    async def put(self, path: str, data: bytes, content_type: str) -> None:
        self.objects[path] = data

    async def get(self, path: str) -> bytes:
        return self.objects[path]

    async def delete(self, paths: Sequence[str]) -> None:
        for path in paths:
            self.objects.pop(path, None)

    async def signed_urls(self, paths: Sequence[str], ttl_seconds: int) -> dict[str, str]:
        self.signing_calls.append(list(paths))
        if self.fail_signing:
            raise ConnectionError("storage is saturated")
        return {path: f"https://storage.test/{path}?token=abc" for path in paths}


def make_record(image_id: int, image: NewImage) -> ImageRecord:
    return ImageRecord(
        id=image_id,
        user_id=image.user_id,
        filename=image.filename,
        original_path=image.original_path,
        thumbnail_path=image.thumbnail_path,
        mime_type=image.mime_type,
        size_bytes=image.size_bytes,
        width=image.width,
        height=image.height,
        content_sha256=image.content_sha256,
        uploaded_at=datetime(2026, 10, 20, 8, 30, tzinfo=UTC),
        status=AnalysisStatus.PENDING,
        tags=(),
        description=None,
        colors=(),
        error=None,
    )


class InMemoryImageRepo:
    """Keeps the caller's images by hash, like the real repo behind RLS.

    A bump moves an image to the front of `order`, the way it does in the gallery.
    """

    def __init__(self) -> None:
        self.inserted: list[NewImage] = []
        self.by_hash: dict[str, ImageRecord] = {}
        self.order: list[int] = []

    async def insert(self, image: NewImage) -> ImageRecord:
        self.inserted.append(image)
        record = make_record(41 + len(self.inserted), image)
        self.by_hash[image.content_sha256] = record
        self.order.insert(0, record.id)
        return record

    async def find_by_sha256(self, sha256s: Sequence[str]) -> dict[str, ImageRecord]:
        return {sha: self.by_hash[sha] for sha in sha256s if sha in self.by_hash}

    async def count_uploaded_since(self, since: datetime) -> int:
        # Every image these tests insert was uploaded just now, so all of them are in the window.
        return len(self.inserted)

    async def bump(self, image_id: int) -> ImageRecord | None:
        record = next((r for r in self.by_hash.values() if r.id == image_id), None)
        if record is not None:
            self.order.remove(image_id)
            self.order.insert(0, image_id)
        return record

    async def bump_many(self, image_ids: Sequence[int]) -> list[ImageRecord]:
        bumped = []
        for image_id in image_ids:
            record = await self.bump(image_id)
            if record is not None:
                bumped.append(record)
        return bumped


@pytest.fixture
def store() -> InMemoryObjectStore:
    return InMemoryObjectStore()


@pytest.fixture
def repo() -> InMemoryImageRepo:
    return InMemoryImageRepo()


@pytest.fixture
def make_app(repo: InMemoryImageRepo, store: InMemoryObjectStore) -> Callable[..., FastAPI]:
    """Build the real app with the Supabase adapters replaced by in-memory ones."""

    def build(*, signed_in: bool = True, **settings: Any) -> FastAPI:
        # gallery.main builds the app when it is imported, and that reads the settings. Import it
        # here, after conftest's autouse fixture has set them, instead of at module level.
        from gallery.main import create_app

        app = create_app()
        app.dependency_overrides[deps.get_settings] = lambda: Settings(**settings)
        app.dependency_overrides[deps.get_image_repo] = lambda: repo
        app.dependency_overrides[deps.get_object_store] = lambda: store
        if signed_in:
            user = CurrentUser(id=USER_ID, email="user@example.test", access_token="test-token")
            app.dependency_overrides[deps.get_current_user] = lambda: user
        return app

    return build


@pytest.fixture
def client(make_app: Callable[..., FastAPI]) -> TestClient:
    return TestClient(make_app(), raise_server_exceptions=False)


def encode(image: Image.Image, image_format: str) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format=image_format)
    return buffer.getvalue()


def post_file(
    client: TestClient, data: bytes, filename: str = "beach.jpg", content_type: str = "image/jpeg"
) -> Any:
    return client.post(URL, files={"file": (filename, data, content_type)})


def a_jpeg() -> bytes:
    return encode(Image.new("RGB", (640, 480), "teal"), "JPEG")


def test_an_upload_answers_201_with_the_image_in_the_api_shape(client: TestClient) -> None:
    response = post_file(client, a_jpeg())

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "id",
        "filename",
        "uploaded_at",
        "width",
        "height",
        "thumbnail_url",
        "original_url",
        "analysis",
        "score",
    }
    assert body["id"] == 42
    assert body["filename"] == "beach.jpg"
    assert body["width"] == 640
    assert body["height"] == 480
    assert body["uploaded_at"] == "2026-10-20T08:30:00Z"
    assert body["thumbnail_url"].startswith("https://storage.test/")
    assert body["thumbnail_url"].endswith("/thumb.webp?token=abc")
    assert body["original_url"] is None
    assert body["score"] is None
    assert body["analysis"] == {
        "status": "pending",
        "tags": [],
        "description": None,
        "colors": [],
        "error": None,
    }


def test_an_upload_whose_thumbnail_cannot_be_signed_is_still_a_201(
    client: TestClient, repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    store.fail_signing = True

    response = post_file(client, a_jpeg())

    assert response.status_code == 201
    assert response.json()["id"] == 42
    assert response.json()["thumbnail_url"] == ""
    assert len(repo.inserted) == 1
    assert len(store.objects) == 2


def test_a_png_upload_is_accepted(client: TestClient) -> None:
    response = post_file(
        client, encode(Image.new("RGBA", (30, 20)), "PNG"), "logo.png", "image/png"
    )

    assert response.status_code == 201
    assert (response.json()["width"], response.json()["height"]) == (30, 20)


def test_the_image_is_stored_under_the_signed_in_users_folder(
    client: TestClient, repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    post_file(client, a_jpeg())

    assert len(store.objects) == 2
    assert all(path.startswith(f"{USER_ID}/") for path in store.objects)
    assert repo.inserted[0].user_id == USER_ID


def test_the_response_does_not_expose_storage_paths_or_the_owner(client: TestClient) -> None:
    body = post_file(client, a_jpeg()).json()

    assert "original_path" not in body
    assert "thumbnail_path" not in body
    assert "user_id" not in body
    assert "content_sha256" not in body


def test_a_file_that_is_not_an_image_is_a_415_in_the_error_shape(client: TestClient) -> None:
    # The client calls it a JPEG. Only the bytes count.
    response = post_file(client, b"just some text", "photo.jpg", "image/jpeg")

    assert response.status_code == 415
    assert response.json() == {
        "error": {"code": "unsupported_type", "message": "Only JPEG and PNG images are accepted."}
    }


@pytest.mark.parametrize("image_format", ["GIF", "WEBP"])
def test_other_image_formats_are_a_415(client: TestClient, image_format: str) -> None:
    response = post_file(
        client, encode(Image.new("RGB", (8, 8)), image_format), "pic.png", "image/png"
    )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_type"


def test_a_corrupt_image_is_a_415(client: TestClient) -> None:
    response = post_file(client, a_jpeg()[:100])

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "corrupt"


def test_a_file_over_the_limit_is_a_413_in_the_error_shape(
    make_app: Callable[..., FastAPI],
) -> None:
    client = TestClient(make_app(MAX_UPLOAD_MB=1), raise_server_exceptions=False)

    response = post_file(client, b"\0" * (MEGABYTE + 1))

    assert response.status_code == 413
    assert response.json() == {
        "error": {"code": "too_large", "message": "The file is larger than 1 MB."}
    }


def test_a_file_exactly_at_the_limit_is_not_too_large(make_app: Callable[..., FastAPI]) -> None:
    client = TestClient(make_app(MAX_UPLOAD_MB=1), raise_server_exceptions=False)

    response = post_file(client, b"\0" * MEGABYTE)

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_type"


def test_an_image_with_too_many_pixels_is_a_413(make_app: Callable[..., FastAPI]) -> None:
    client = TestClient(make_app(MAX_IMAGE_PIXELS=1000), raise_server_exceptions=False)

    response = post_file(client, encode(Image.new("RGB", (40, 30)), "PNG"), "big.png", "image/png")

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "too_many_pixels"


def test_nothing_is_stored_for_a_rejected_file(
    client: TestClient, repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    post_file(client, b"just some text")

    assert store.objects == {}
    assert repo.inserted == []


class SpyService:
    """Stands in for UploadService to see how many bytes the route read."""

    max_bytes = 100

    def __init__(self) -> None:
        self.received: list[int] = []

    async def upload(self, user_id: UUID, raw_filename: str | None, data: bytes) -> Any:
        self.received.append(len(data))
        raise AssertionError("stop here")


def test_the_route_reads_at_most_one_byte_over_the_limit(make_app: Callable[..., FastAPI]) -> None:
    spy = SpyService()
    app = make_app()
    app.dependency_overrides[uploads.get_upload_service] = lambda: spy

    post_file(TestClient(app, raise_server_exceptions=False), b"x" * 5000)

    assert spy.received == [spy.max_bytes + 1]


def test_a_request_without_a_file_is_a_422_in_the_error_shape(client: TestClient) -> None:
    response = client.post(URL, data={"note": "no file here"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer not-a-token"}])
def test_an_upload_without_a_valid_token_is_a_401_and_stores_nothing(
    make_app: Callable[..., FastAPI],
    repo: InMemoryImageRepo,
    store: InMemoryObjectStore,
    headers: dict[str, str],
) -> None:
    client = TestClient(make_app(signed_in=False), raise_server_exceptions=False)

    response = client.post(URL, files={"file": ("a.jpg", a_jpeg(), "image/jpeg")}, headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert store.objects == {}
    assert repo.inserted == []


def test_the_upload_is_documented_in_the_openapi_schema(client: TestClient) -> None:
    operation = client.get("/api/openapi.json").json()["paths"][URL]["post"]

    assert set(operation["responses"]) >= {"200", "201", "413", "415", "422"}
    for status in ("200", "201"):
        schema = operation["responses"][status]["content"]["application/json"]["schema"]
        assert schema == {"$ref": "#/components/schemas/ImageOut"}


# --- duplicates ----------------------------------------------------------------------------


def another_jpeg() -> bytes:
    return encode(Image.new("RGB", (320, 200), "crimson"), "JPEG")


def test_uploading_a_file_the_caller_already_has_answers_200_with_the_same_image(
    client: TestClient, repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    first = post_file(client, a_jpeg())

    second = post_file(client, a_jpeg(), "copy-of-beach.jpg")

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json() == first.json()
    assert len(repo.inserted) == 1
    assert len(store.objects) == 2


def test_a_duplicate_upload_moves_the_image_to_the_top(
    client: TestClient, repo: InMemoryImageRepo
) -> None:
    x = post_file(client, a_jpeg(), "x.jpg")
    y = post_file(client, another_jpeg(), "y.jpg")
    assert repo.order == [y.json()["id"], x.json()["id"]]

    again = post_file(client, a_jpeg(), "x.jpg")

    assert [x.status_code, y.status_code, again.status_code] == [201, 201, 200]
    assert again.json()["id"] == x.json()["id"]
    assert repo.order == [x.json()["id"], y.json()["id"]]


def test_a_duplicate_does_not_need_the_thumbnail_to_be_signable(
    client: TestClient, store: InMemoryObjectStore
) -> None:
    post_file(client, a_jpeg())
    store.fail_signing = True

    response = post_file(client, a_jpeg())

    assert response.status_code == 200
    assert response.json()["thumbnail_url"] == ""


# --- POST /api/v1/images/{id}/bump ---------------------------------------------------------


def test_bump_answers_200_with_the_image_and_moves_it_to_the_top(
    client: TestClient, repo: InMemoryImageRepo
) -> None:
    x = post_file(client, a_jpeg()).json()
    y = post_file(client, another_jpeg()).json()

    response = client.post(f"{URL}/{x['id']}/bump")

    assert response.status_code == 200
    assert response.json() == x
    assert response.json()["thumbnail_url"].endswith("/thumb.webp?token=abc")
    assert repo.order == [x["id"], y["id"]]


def test_bumping_a_missing_or_foreign_image_is_a_404_in_the_error_shape(
    client: TestClient,
) -> None:
    response = client.post(f"{URL}/9999/bump")

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "Image not found."}}


def test_bump_with_a_non_numeric_id_is_a_422(client: TestClient) -> None:
    response = client.post(f"{URL}/abc/bump")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# --- POST /api/v1/images/bump --------------------------------------------------------------

BUMP_URL = f"{URL}/bump"


def seed_images(repo: InMemoryImageRepo, count: int) -> list[int]:
    """Give the caller `count` images, the first one the oldest, and return their ids."""
    ids = []
    for position in range(count):
        image_id = 100 + position
        folder = f"{USER_ID}/{image_id}"
        record = make_record(
            image_id,
            NewImage(
                user_id=USER_ID,
                filename=f"photo-{image_id}.jpg",
                original_path=f"{folder}/original.jpg",
                thumbnail_path=f"{folder}/thumb.webp",
                mime_type="image/jpeg",
                size_bytes=1234,
                width=640,
                height=480,
                content_sha256=f"{image_id:064x}",
            ),
        )
        repo.by_hash[record.content_sha256] = record
        repo.order.insert(0, image_id)
        ids.append(image_id)
    return ids


def test_batch_bump_answers_200_with_the_images_in_the_order_given(
    client: TestClient, repo: InMemoryImageRepo
) -> None:
    first, second, third = seed_images(repo, 3)

    response = client.post(BUMP_URL, json={"image_ids": [first, third, second]})

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [first, third, second]
    assert all(
        item["thumbnail_url"].endswith("/thumb.webp?token=abc") for item in response.json()["items"]
    )
    # The last id of the batch is on top of the gallery.
    assert repo.order[0] == second


def test_batch_bump_returns_the_same_shape_as_the_single_bump(
    client: TestClient, repo: InMemoryImageRepo
) -> None:
    [image_id] = seed_images(repo, 1)

    single = client.post(f"{URL}/{image_id}/bump").json()
    batch = client.post(BUMP_URL, json={"image_ids": [image_id]}).json()

    assert batch == {"items": [single]}


def test_batch_bump_signs_every_thumbnail_in_one_call(
    client: TestClient, repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    ids = seed_images(repo, 50)

    response = client.post(BUMP_URL, json={"image_ids": ids})

    assert response.status_code == 200
    assert len(response.json()["items"]) == 50
    assert len(store.signing_calls) == 1
    assert len(store.signing_calls[0]) == 50


def test_batch_bump_leaves_out_missing_and_foreign_ids(
    client: TestClient, repo: InMemoryImageRepo
) -> None:
    mine, other_mine = seed_images(repo, 2)

    response = client.post(BUMP_URL, json={"image_ids": [mine, 9999, other_mine]})

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [mine, other_mine]


def test_batch_bump_of_ids_that_are_all_missing_is_an_empty_list_not_a_404(
    client: TestClient, store: InMemoryObjectStore
) -> None:
    response = client.post(BUMP_URL, json={"image_ids": [9998, 9999]})

    assert response.status_code == 200
    assert response.json() == {"items": []}
    assert store.signing_calls == []


def test_batch_bump_with_a_failing_signer_still_moves_the_images(
    client: TestClient, repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    [image_id] = seed_images(repo, 1)
    store.fail_signing = True

    response = client.post(BUMP_URL, json={"image_ids": [image_id]})

    assert response.status_code == 200
    assert response.json()["items"][0]["thumbnail_url"] == ""


def test_batch_bump_takes_at_most_50_ids(client: TestClient, repo: InMemoryImageRepo) -> None:
    ids = seed_images(repo, 51)

    assert client.post(BUMP_URL, json={"image_ids": ids[:50]}).status_code == 200

    too_many = client.post(BUMP_URL, json={"image_ids": ids})
    assert too_many.status_code == 422
    assert too_many.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"image_ids": []},
        {"image_ids": "7"},
        {"image_ids": [0]},
        {"image_ids": [-1]},
        {"image_ids": [2**63]},
        {"image_ids": ["abc"]},
        {"image_ids": [1.5]},
        {"image_ids": [None]},
        {"ids": [1]},
    ],
    ids=repr,
)
def test_batch_bump_rejects_a_malformed_body(client: TestClient, body: dict[str, Any]) -> None:
    response = client.post(BUMP_URL, json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# --- POST /api/v1/images/lookup ------------------------------------------------------------

LOOKUP_URL = f"{URL}/lookup"


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_lookup_maps_known_hashes_to_image_ids_and_leaves_out_unknown_ones(
    client: TestClient,
) -> None:
    x = post_file(client, a_jpeg()).json()
    unknown = "0" * 64

    response = client.post(
        LOOKUP_URL, json={"content_sha256": [sha256_of(a_jpeg()), unknown, sha256_of(a_jpeg())]}
    )

    assert response.status_code == 200
    assert response.json() == {"matches": {sha256_of(a_jpeg()): x["id"]}}


def test_lookup_with_no_matches_is_an_empty_map(client: TestClient) -> None:
    response = client.post(LOOKUP_URL, json={"content_sha256": ["a" * 64]})

    assert response.status_code == 200
    assert response.json() == {"matches": {}}


def test_lookup_accepts_exactly_50_hashes(client: TestClient) -> None:
    hashes = [f"{index:064x}" for index in range(50)]

    response = client.post(LOOKUP_URL, json={"content_sha256": hashes})

    assert response.status_code == 200


@pytest.mark.parametrize(
    "hashes",
    [
        [],
        [f"{index:064x}" for index in range(51)],
        ["a" * 63],
        ["a" * 65],
        ["A" * 64],
        ["g" * 64],
        [" " + "a" * 63],
        ["a" * 64 + "\n"],
        [123],
        ["a" * 64, "not a hash"],
    ],
    ids=[
        "empty",
        "51-items",
        "too-short",
        "too-long",
        "uppercase",
        "not-hex",
        "leading-space",
        "trailing-newline",
        "not-a-string",
        "one-bad-among-good",
    ],
)
def test_lookup_rejects_bad_hashes_with_a_422_in_the_error_shape(
    client: TestClient, hashes: list[Any]
) -> None:
    response = client.post(LOOKUP_URL, json={"content_sha256": hashes})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("body", [{}, {"hashes": ["a" * 64]}, {"content_sha256": "a" * 64}])
def test_lookup_rejects_a_malformed_body(client: TestClient, body: dict[str, Any]) -> None:
    response = client.post(LOOKUP_URL, json=body)

    assert response.status_code == 422


@pytest.mark.parametrize("path", [LOOKUP_URL, f"{URL}/42/bump", BUMP_URL])
def test_lookup_and_bump_without_a_token_are_a_401(
    make_app: Callable[..., FastAPI], path: str
) -> None:
    client = TestClient(make_app(signed_in=False), raise_server_exceptions=False)

    response = client.post(path, json={"content_sha256": ["a" * 64], "image_ids": [42]})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_lookup_and_bump_are_documented_in_the_openapi_schema(client: TestClient) -> None:
    paths = client.get("/api/openapi.json").json()["paths"]

    lookup = paths[LOOKUP_URL]["post"]
    assert set(lookup["responses"]) >= {"200", "422"}
    assert (
        lookup["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/LookupResponse"
    )
    assert set(paths[f"{URL}/{{image_id}}/bump"]["post"]["responses"]) >= {"200", "404", "422"}

    batch = paths[BUMP_URL]["post"]
    assert set(batch["responses"]) >= {"200", "422"}
    assert (
        batch["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/BumpResponse"
    )
    request = client.get("/api/openapi.json").json()["components"]["schemas"]["BumpRequest"]
    assert request["properties"]["image_ids"]["minItems"] == 1
    assert request["properties"]["image_ids"]["maxItems"] == 50


# --- the daily quota, through the route -----------------------------------------------------------


def png_in_colour(index: int) -> bytes:
    """A tiny PNG in a colour unlike any other in the test. PNG is lossless, so no upload is a
    duplicate of another. (A solid-colour JPEG quantises near colours to the same bytes.)"""
    buffer = BytesIO()
    Image.new("RGB", (8, 8), (index % 256, index // 256, 7)).save(buffer, format="PNG")
    return buffer.getvalue()


def upload_colour(client: TestClient, index: int) -> Any:
    return post_file(client, png_in_colour(index), "photo.png", "image/png")


def test_the_301st_upload_in_a_day_is_refused_with_429_and_a_retry_after(
    client: TestClient, store: InMemoryObjectStore
) -> None:
    # No quota is set here, so this runs at the production default of 300.
    for index in range(300):
        assert upload_colour(client, index).status_code == 201
    objects_before = dict(store.objects)

    response = upload_colour(client, 300)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "86400"
    assert response.json() == {
        "error": {
            "code": "quota_exceeded",
            "message": "You've reached today's upload limit of 300 images. Try again tomorrow.",
        }
    }
    # The refusal comes before any Storage write.
    assert store.objects == objects_before


OTHER_USER_ID = UUID("3f1c9a7e-5b2d-4c8e-9a6f-0e1d2c3b4a59")


def test_a_user_who_is_not_exempt_is_limited_at_the_configured_quota(
    make_app: Callable[..., FastAPI],
) -> None:
    app = make_app(UPLOAD_DAILY_QUOTA=2, UPLOAD_QUOTA_EXEMPT_USER_IDS=str(OTHER_USER_ID))
    client = TestClient(app, raise_server_exceptions=False)

    statuses = [upload_colour(client, index).status_code for index in range(3)]

    assert statuses == [201, 201, 429]


def test_an_exempt_user_id_is_never_limited(make_app: Callable[..., FastAPI]) -> None:
    app = make_app(UPLOAD_DAILY_QUOTA=2, UPLOAD_QUOTA_EXEMPT_USER_IDS=f"{OTHER_USER_ID}, {USER_ID}")
    client = TestClient(app, raise_server_exceptions=False)

    statuses = [upload_colour(client, index).status_code for index in range(4)]

    assert statuses == [201, 201, 201, 201]


def test_an_email_no_longer_exempts_anyone(
    make_app: Callable[..., FastAPI], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server .env that still lists the owner's email doesn't exempt whoever signs in with it."""
    monkeypatch.setenv("UPLOAD_QUOTA_EXEMPT_EMAILS", "owner@example.test")
    app = make_app(UPLOAD_DAILY_QUOTA=2)
    app.dependency_overrides[deps.get_current_user] = lambda: CurrentUser(
        id=USER_ID, email="owner@example.test", access_token="test-token"
    )
    client = TestClient(app, raise_server_exceptions=False)

    statuses = [upload_colour(client, index).status_code for index in range(3)]

    assert statuses == [201, 201, 429]
