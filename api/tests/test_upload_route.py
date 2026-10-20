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

    async def put(self, path: str, data: bytes, content_type: str) -> None:
        self.objects[path] = data

    async def get(self, path: str) -> bytes:
        return self.objects[path]

    async def delete(self, paths: Sequence[str]) -> None:
        for path in paths:
            self.objects.pop(path, None)

    async def signed_urls(self, paths: Sequence[str], ttl_seconds: int) -> dict[str, str]:
        return {path: f"https://storage.test/{path}?token=abc" for path in paths}


class InMemoryImageRepo:
    def __init__(self) -> None:
        self.inserted: list[NewImage] = []

    async def insert(self, image: NewImage) -> ImageRecord:
        self.inserted.append(image)
        return ImageRecord(
            id=41 + len(self.inserted),
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

    assert set(operation["responses"]) >= {"201", "413", "415", "422"}
