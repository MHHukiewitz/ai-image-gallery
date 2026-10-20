"""Gallery routes with the Supabase adapters overridden by mocks: status codes and response shapes.

The real service and error handlers run. Only the repo and the object store are replaced, so
none of these tests touches Supabase or needs a token.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from gallery.api import deps
from gallery.domain.models import (
    AllImages,
    AnalysisStatus,
    ColorQuery,
    ImageRecord,
    Page,
    SimilarTo,
    TextQuery,
)
from gallery.ports import ImageRepo, ObjectStore

OWNER_ID = UUID("7b9e2c4a-1d3f-4e5a-8b6c-9d0e1f2a3b4c")
NOT_FOUND = {"error": {"code": "not_found", "message": "Image not found."}}


def make_record(image_id: int) -> ImageRecord:
    folder = f"{OWNER_ID}/{image_id}"
    return ImageRecord(
        id=image_id,
        user_id=OWNER_ID,
        filename=f"photo-{image_id}.jpg",
        original_path=f"{folder}/original.jpg",
        thumbnail_path=f"{folder}/thumb.webp",
        mime_type="image/jpeg",
        size_bytes=1234,
        width=640,
        height=480,
        content_sha256="a" * 64,
        uploaded_at=datetime(2026, 10, 20, 12, 0, tzinfo=UTC),
        status=AnalysisStatus.DONE,
        tags=("beach",),
        description="A beach at dusk.",
        colors=("#336699",),
        error=None,
    )


def signed(path: str) -> str:
    return f"https://signed.example.test/{path}"


def signed_thumbnails(*records: ImageRecord) -> dict[str, str]:
    return {record.thumbnail_path: signed(record.thumbnail_path) for record in records}


@pytest.fixture
def repo() -> AsyncMock:
    # spec keeps the mock to the port's methods, so a typo in a test fails loudly.
    mock = AsyncMock(spec=ImageRepo)
    mock.list.return_value = Page(items=[], next_offset=None)
    return mock


@pytest.fixture
def store() -> AsyncMock:
    return AsyncMock(spec=ObjectStore)


@pytest.fixture
def client(repo: AsyncMock, store: AsyncMock) -> Iterator[TestClient]:
    # gallery.main builds the app when it is imported, and that reads the settings. Import it
    # here, after conftest's autouse fixture has set them, instead of at module level.
    from gallery.main import create_app

    app = create_app()
    app.dependency_overrides[deps.get_image_repo] = lambda: repo
    app.dependency_overrides[deps.get_object_store] = lambda: store
    yield TestClient(app, raise_server_exceptions=False)


# --- GET /api/v1/images --------------------------------------------------------------------


def test_a_page_with_more_to_come_sets_next_offset(
    client: TestClient, repo: AsyncMock, store: AsyncMock
) -> None:
    newest, older = make_record(3), make_record(2)
    repo.list.return_value = Page(items=[newest, older], next_offset=2)
    store.signed_urls.return_value = signed_thumbnails(newest, older)

    response = client.get("/api/v1/images", params={"limit": 2})

    assert response.status_code == 200
    body = response.json()
    assert body["next_offset"] == 2
    assert [item["id"] for item in body["items"]] == [3, 2]
    assert body["items"][0]["thumbnail_url"] == signed(newest.thumbnail_path)
    assert body["items"][0]["original_url"] is None
    assert body["items"][0]["score"] is None
    repo.list.assert_awaited_once_with(AllImages(), limit=2, offset=0)


def test_the_last_page_has_a_null_next_offset(
    client: TestClient, repo: AsyncMock, store: AsyncMock
) -> None:
    record = make_record(1)
    repo.list.return_value = Page(items=[record], next_offset=None)
    store.signed_urls.return_value = signed_thumbnails(record)

    response = client.get("/api/v1/images", params={"offset": 40})

    assert response.status_code == 200
    assert response.json()["next_offset"] is None
    repo.list.assert_awaited_once_with(AllImages(), limit=20, offset=40)


def test_an_empty_gallery_is_an_empty_page(client: TestClient, store: AsyncMock) -> None:
    response = client.get("/api/v1/images")

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_offset": None}
    store.signed_urls.assert_awaited_once_with([], 3600)


def test_a_thumbnail_the_store_cannot_sign_is_an_empty_url(
    client: TestClient, repo: AsyncMock, store: AsyncMock
) -> None:
    record = make_record(1)
    repo.list.return_value = Page(items=[record], next_offset=None)
    store.signed_urls.return_value = {}

    response = client.get("/api/v1/images")

    assert response.status_code == 200
    assert response.json()["items"][0]["thumbnail_url"] == ""


def test_q_is_stripped_and_searches_by_text(client: TestClient, repo: AsyncMock) -> None:
    repo.list.return_value = Page(items=[], next_offset=None)

    client.get("/api/v1/images", params={"q": "  beach "})

    repo.list.assert_awaited_once_with(TextQuery("beach"), limit=20, offset=0)


def test_similar_to_searches_by_image(client: TestClient, repo: AsyncMock) -> None:
    repo.get.return_value = make_record(7)
    repo.list.return_value = Page(items=[], next_offset=None)

    client.get("/api/v1/images", params={"similar_to": 7})

    repo.list.assert_awaited_once_with(SimilarTo(7), limit=20, offset=0)


def test_color_is_lowercased_before_the_search(client: TestClient, repo: AsyncMock) -> None:
    repo.list.return_value = Page(items=[], next_offset=None)

    client.get("/api/v1/images", params={"color": "#AABBCC"})

    repo.list.assert_awaited_once_with(ColorQuery("#aabbcc"), limit=20, offset=0)


@pytest.mark.parametrize(
    "params",
    [{"q": "beach"}, {"similar_to": 7}, {"color": "#336699"}],
    ids=["text", "similar", "color"],
)
def test_a_search_mode_returns_its_page_with_scores(
    client: TestClient, repo: AsyncMock, store: AsyncMock, params: dict[str, object]
) -> None:
    # The search itself runs in the repo, which this test replaces. The route passes the page on.
    found = replace(make_record(3), score=0.83)
    repo.get.return_value = make_record(7)  # the target of a similar-to search is visible
    repo.list.return_value = Page(items=[found], next_offset=None)
    store.signed_urls.return_value = signed_thumbnails(found)

    response = client.get("/api/v1/images", params=params)

    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == 3
    assert response.json()["items"][0]["score"] == 0.83


@pytest.mark.parametrize(
    "params",
    [
        {"q": "beach", "color": "#336699"},
        {"q": "beach", "similar_to": 7},
        {"similar_to": 7, "color": "#336699"},
    ],
    ids=["text-and-color", "text-and-similar", "similar-and-color"],
)
def test_two_search_filters_at_once_are_a_422(
    client: TestClient, repo: AsyncMock, params: dict[str, object]
) -> None:
    response = client.get("/api/v1/images", params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert "only one of q, similar_to, color" in response.json()["error"]["message"]
    repo.list.assert_not_awaited()


def test_a_color_that_is_not_hex_is_a_422(client: TestClient, repo: AsyncMock) -> None:
    response = client.get("/api/v1/images", params={"color": "red"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    repo.list.assert_not_awaited()


# --- GET /api/v1/images/{id} ---------------------------------------------------------------


def test_the_detail_route_returns_the_thumbnail_and_original_urls(
    client: TestClient, repo: AsyncMock, store: AsyncMock
) -> None:
    record = make_record(5)
    repo.get.return_value = record
    store.signed_urls.return_value = {
        record.thumbnail_path: signed(record.thumbnail_path),
        record.original_path: signed(record.original_path),
    }

    response = client.get("/api/v1/images/5")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 5
    assert body["thumbnail_url"] == signed(record.thumbnail_path)
    assert body["original_url"] == signed(record.original_path)
    assert body["analysis"] == {
        "status": "done",
        "tags": ["beach"],
        "description": "A beach at dusk.",
        "colors": ["#336699"],
        "error": None,
    }
    repo.get.assert_awaited_once_with(5)


def test_a_missing_image_is_a_404_in_the_error_shape(client: TestClient, repo: AsyncMock) -> None:
    repo.get.return_value = None

    response = client.get("/api/v1/images/5")

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


# --- DELETE /api/v1/images/{id} ------------------------------------------------------------


def test_delete_answers_204_with_no_body_and_removes_both_objects(
    client: TestClient, repo: AsyncMock, store: AsyncMock
) -> None:
    record = make_record(5)
    repo.delete.return_value = record

    response = client.delete("/api/v1/images/5")

    assert response.status_code == 204
    assert response.content == b""
    store.delete.assert_awaited_once_with([record.original_path, record.thumbnail_path])


def test_deleting_a_missing_image_is_a_404_and_touches_no_objects(
    client: TestClient, repo: AsyncMock, store: AsyncMock
) -> None:
    repo.delete.return_value = None

    response = client.delete("/api/v1/images/5")

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    store.delete.assert_not_awaited()


# --- POST /api/v1/images/{id}/reanalyze ----------------------------------------------------


def test_reanalyze_answers_202_with_no_body(client: TestClient, repo: AsyncMock) -> None:
    repo.request_reanalysis.return_value = True

    response = client.post("/api/v1/images/5/reanalyze")

    assert response.status_code == 202
    assert response.content == b""
    repo.request_reanalysis.assert_awaited_once_with(5)


def test_reanalyze_is_a_404_when_the_image_is_missing_or_busy(
    client: TestClient, repo: AsyncMock
) -> None:
    repo.request_reanalysis.return_value = False

    response = client.post("/api/v1/images/5/reanalyze")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "Image not found, or its analysis is already running.",
        }
    }


# --- authentication ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/images"),
        ("GET", "/api/v1/images/5"),
        ("DELETE", "/api/v1/images/5"),
        ("POST", "/api/v1/images/5/reanalyze"),
    ],
)
def test_every_gallery_route_needs_a_bearer_token(method: str, path: str) -> None:
    # No overrides here: the real auth dependency has to refuse the request first.
    from gallery.main import create_app

    anonymous = TestClient(create_app(), raise_server_exceptions=False)

    response = anonymous.request(method, path)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
