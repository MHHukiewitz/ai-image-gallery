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
from PIL.ExifTags import Base

from gallery.api import deps
from gallery.api.body_limit import MULTIPART_ALLOWANCE_BYTES
from gallery.api.deps import CurrentUser
from gallery.api.routes import uploads
from gallery.config import Settings, get_settings
from gallery.domain.errors import QuotaExceededError
from gallery.domain.models import AnalysisStatus, ImageRecord, NewImage

USER_ID = UUID("7b9e2c4a-1d3f-4e5a-8b6c-9d0e1f2a3b4c")
URL = "/api/v1/images"
MEGABYTE = 1024 * 1024


class InMemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_signing = False
        self.signing_calls: list[list[str]] = []
        # Raised by the put after `puts_before_error` successful ones, like Storage refusing an
        # object past the storage quota.
        self.put_error: Exception | None = None
        self.puts_before_error = 0

    async def put(self, path: str, data: bytes, content_type: str) -> None:
        if self.put_error is not None:
            if self.puts_before_error == 0:
                raise self.put_error
            self.puts_before_error -= 1
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
        # Raised by the next insert, like the database refusing a row.
        self.insert_error: Exception | None = None

    async def insert(self, image: NewImage) -> ImageRecord:
        if self.insert_error is not None:
            raise self.insert_error
        self.inserted.append(image)
        record = make_record(41 + len(self.inserted), image)
        # The real repo finds an image by either hash.
        self.by_hash[image.content_sha256] = record
        self.by_hash[image.stored_sha256] = record
        self.order.insert(0, record.id)
        return record

    async def find_by_sha256(self, user_id: UUID, sha256s: Sequence[str]) -> dict[str, ImageRecord]:
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


def test_a_text_field_named_file_is_a_422(client: TestClient) -> None:
    response = client.post(URL, data={"file": "not a file"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# --- the body is checked before it is read ----------------------------------------------------


class StreamingClient:
    """Sends a request body to the app in chunks, at the ASGI level, and collects the answer.

    TestClient reads the whole body before it calls the app. This shows how much of it the app
    actually read, and can leave out Content-Length like a chunked upload does.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.chunks_read = 0
        self.status: int | None = None
        self.body = b""

    async def post(
        self, app: FastAPI, *, content_length: int | None, token: str | None = None
    ) -> None:
        headers = [(b"content-type", b"multipart/form-data; boundary=boundary")]
        if content_length is not None:
            headers.append((b"content-length", str(content_length).encode()))
        if token is not None:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": URL,
            "raw_path": URL.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": headers,
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        }
        await app(scope, self.receive, self.send)

    async def receive(self) -> dict[str, Any]:
        if self.chunks_read == len(self.chunks):
            return {"type": "http.disconnect"}
        self.chunks_read += 1
        return {
            "type": "http.request",
            "body": self.chunks[self.chunks_read - 1],
            "more_body": self.chunks_read < len(self.chunks),
        }

    async def send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
        else:
            self.body += message.get("body", b"")


def multipart_chunks(file_size: int, chunk_size: int = 64 * 1024) -> list[bytes]:
    """A multipart body with one `file` part of `file_size` bytes, in chunks."""
    head = (
        b"--boundary\r\n"
        b'Content-Disposition: form-data; name="file"; filename="big.jpg"\r\n'
        b"Content-Type: image/jpeg\r\n\r\n"
    )
    body = head + b"\0" * file_size + b"\r\n--boundary--\r\n"
    return [body[i : i + chunk_size] for i in range(0, len(body), chunk_size)]


@pytest.fixture
def one_megabyte_body_limit(monkeypatch: pytest.MonkeyPatch) -> int:
    """Set MAX_UPLOAD_MB=1 where the body limit reads it, and return that limit in bytes."""
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    get_settings.cache_clear()
    return MEGABYTE + MULTIPART_ALLOWANCE_BYTES


@pytest.mark.parametrize("token", [None, "not-a-token"])
async def test_an_upload_without_a_valid_token_is_refused_before_its_body_is_read(
    make_app: Callable[..., FastAPI], token: str | None
) -> None:
    chunks = multipart_chunks(5 * MEGABYTE)
    client = StreamingClient(chunks)

    await client.post(make_app(signed_in=False), content_length=sum(map(len, chunks)), token=token)

    assert client.status == 401
    assert client.chunks_read == 0


def test_a_body_over_the_limit_is_a_413_and_the_route_never_runs(
    make_app: Callable[..., FastAPI], one_megabyte_body_limit: int
) -> None:
    service_built: list[bool] = []
    app = make_app()
    app.dependency_overrides[uploads.get_upload_service] = lambda: service_built.append(True)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        URL,
        content=b"\0" * (one_megabyte_body_limit + 1),
        headers={"Content-Type": "multipart/form-data; boundary=boundary"},
    )

    assert response.status_code == 413
    assert response.json() == {
        "error": {"code": "too_large", "message": "The file is larger than 1 MB."}
    }
    assert service_built == []


async def test_a_chunked_body_over_the_limit_is_cut_off_with_a_413(
    make_app: Callable[..., FastAPI], one_megabyte_body_limit: int
) -> None:
    spy = SpyService()
    app = make_app()
    app.dependency_overrides[uploads.get_upload_service] = lambda: spy
    chunks = multipart_chunks(20 * MEGABYTE)
    client = StreamingClient(chunks)

    await client.post(app, content_length=None)

    assert client.status == 413
    assert client.body == (
        b'{"error":{"code":"too_large","message":"The file is larger than 1 MB."}}'
    )
    assert spy.received == []
    assert client.chunks_read < len(chunks) // 10


async def test_a_chunked_upload_within_the_limit_is_accepted(
    make_app: Callable[..., FastAPI], repo: InMemoryImageRepo
) -> None:
    jpeg = a_jpeg()
    body = (
        b"--boundary\r\n"
        b'Content-Disposition: form-data; name="file"; filename="beach.jpg"\r\n'
        b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n--boundary--\r\n"
    )
    client = StreamingClient([body[i : i + 1000] for i in range(0, len(body), 1000)])

    await client.post(make_app(), content_length=None)

    assert client.status == 201
    assert [image.size_bytes for image in repo.inserted] == [len(jpeg)]


def test_the_upload_is_documented_in_the_openapi_schema(client: TestClient) -> None:
    schema = client.get("/api/openapi.json").json()
    operation = schema["paths"][URL]["post"]

    assert set(operation["responses"]) >= {"200", "201", "413", "415", "422"}
    form = operation["requestBody"]["content"]["multipart/form-data"]["schema"]
    name = form["$ref"].removeprefix("#/components/schemas/")
    assert schema["components"]["schemas"][name]["required"] == ["file"]
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


def a_phone_jpeg(make: str = "TestCam") -> bytes:
    """A JPEG with camera EXIF. Another `make` gives the same picture with other metadata."""
    exif = Image.Exif()
    exif[Base.Make] = make
    buffer = BytesIO()
    Image.new("RGB", (640, 480), "teal").save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def stored_original(store: InMemoryObjectStore) -> bytes:
    """The one original in the store: the file a user gets when they download it."""
    [original] = [data for path, data in store.objects.items() if "/original." in path]
    return original


def test_a_downloaded_original_uploaded_again_answers_200_with_the_same_image(
    client: TestClient, repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    first = post_file(client, a_phone_jpeg())
    downloaded = stored_original(store)
    assert sha256_of(downloaded) != sha256_of(a_phone_jpeg())

    second = post_file(client, downloaded, "downloaded.jpg")

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["id"] == first.json()["id"]
    assert len(repo.inserted) == 1


def test_the_same_picture_with_other_metadata_answers_200_with_the_same_image(
    client: TestClient, repo: InMemoryImageRepo
) -> None:
    first = post_file(client, a_phone_jpeg(make="TestCam"))

    second = post_file(client, a_phone_jpeg(make="OtherCam"), "retagged.jpg")

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["id"] == first.json()["id"]
    assert len(repo.inserted) == 1


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
                stored_sha256=f"{image_id:064x}",
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


def test_lookup_finds_an_image_by_the_hash_of_its_stored_file(
    client: TestClient, store: InMemoryObjectStore
) -> None:
    # The browser hashes the file it drops. For a downloaded original, that is the stored file.
    image = post_file(client, a_phone_jpeg()).json()
    downloaded_hash = sha256_of(stored_original(store))

    response = client.post(LOOKUP_URL, json={"content_sha256": [downloaded_hash]})

    assert response.status_code == 200
    assert response.json() == {"matches": {downloaded_hash: image["id"]}}


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


def test_a_refusal_by_the_database_answers_the_same_429_and_leaves_no_objects(
    client: TestClient, repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    # The API's own check passed, then the database refused the row: another upload took the slot.
    message = "You've reached today's upload limit of 300 images. Try again tomorrow."
    repo.insert_error = QuotaExceededError(message, retry_after_seconds=86400)

    response = upload_colour(client, 0)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "86400"
    assert response.json() == {"error": {"code": "quota_exceeded", "message": message}}
    # The objects written before the insert are removed again.
    assert store.objects == {}


# --- the storage quota, through the route --------------------------------------------------------


# The original is refused when the folder is already full; the thumbnail when the original took
# the last slot.
@pytest.mark.parametrize("puts_before_refusal", [0, 1], ids=["original", "thumbnail"])
def test_a_refusal_by_storage_answers_429_with_a_short_retry_after_and_leaves_no_objects(
    client: TestClient,
    repo: InMemoryImageRepo,
    store: InMemoryObjectStore,
    puts_before_refusal: int,
) -> None:
    message = "You've reached your storage limit. Delete some images to make room."
    store.put_error = QuotaExceededError(message, retry_after_seconds=60)
    store.puts_before_error = puts_before_refusal

    response = upload_colour(client, 0)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert response.json() == {"error": {"code": "quota_exceeded", "message": message}}
    # An original stored before the refusal is removed again, and no row points at it.
    assert store.objects == {}
    assert repo.inserted == []


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
