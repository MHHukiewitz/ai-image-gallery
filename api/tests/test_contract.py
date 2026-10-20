"""Contract tests for the frozen API surface that the later tickets build on."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from gallery.api.deps import CurrentUser, get_current_user, get_database_check
from gallery.api.schemas import AnalysisOut, ImageOut, to_image_out
from gallery.domain.models import AnalysisStatus, ImageRecord

OWNER_ID = UUID("7b9e2c4a-1d3f-4e5a-8b6c-9d0e1f2a3b4c")


@pytest.fixture
def client() -> TestClient:
    # gallery.main builds the app when it is imported, and that reads the settings. Import it
    # here, after conftest's autouse fixture has set them, instead of at module level.
    from gallery.main import create_app

    app = create_app()
    # These tests check validation and status codes, not sign-in, so every request is signed in.
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=OWNER_ID, email=None, access_token="test-token"
    )
    return TestClient(app)


def make_record(**overrides: Any) -> ImageRecord:
    fields: dict[str, Any] = {
        "id": 7,
        "user_id": OWNER_ID,
        "filename": "beach.jpg",
        "original_path": f"{OWNER_ID}/abc/original.jpg",
        "thumbnail_path": f"{OWNER_ID}/abc/thumb.webp",
        "mime_type": "image/jpeg",
        "size_bytes": 1234,
        "width": 640,
        "height": 480,
        "content_sha256": "a" * 64,
        "uploaded_at": datetime(2026, 10, 20, 12, 0, tzinfo=UTC),
        "status": AnalysisStatus.DONE,
        "tags": ("beach", "sunset"),
        "description": "A sunset over the sea.",
        "colors": ("#ff8800", "#0044aa"),
        "error": None,
        "score": 0.83,
    }
    return ImageRecord(**(fields | overrides))


def test_to_image_out_maps_every_field() -> None:
    image = to_image_out(
        make_record(),
        thumbnail_url="https://signed.example.test/thumb",
        original_url="https://signed.example.test/original",
    )

    assert image == ImageOut(
        id=7,
        filename="beach.jpg",
        uploaded_at=datetime(2026, 10, 20, 12, 0, tzinfo=UTC),
        width=640,
        height=480,
        thumbnail_url="https://signed.example.test/thumb",
        original_url="https://signed.example.test/original",
        analysis=AnalysisOut(
            status=AnalysisStatus.DONE,
            tags=["beach", "sunset"],
            description="A sunset over the sea.",
            colors=["#ff8800", "#0044aa"],
            error=None,
        ),
        score=0.83,
    )


def test_to_image_out_exposes_only_the_contract_fields() -> None:
    image = to_image_out(make_record(), thumbnail_url="https://signed.example.test/thumb")

    assert set(image.model_dump()) == {
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
    assert set(image.analysis.model_dump()) == {
        "status",
        "tags",
        "description",
        "colors",
        "error",
    }


def test_to_image_out_leaves_detail_and_search_fields_unset_by_default() -> None:
    image = to_image_out(make_record(score=None), thumbnail_url="https://signed.example.test/thumb")

    assert image.original_url is None
    assert image.score is None


def test_to_image_out_carries_a_failed_analysis() -> None:
    record = make_record(
        status=AnalysisStatus.FAILED,
        tags=(),
        description=None,
        colors=(),
        error="provider timed out",
    )

    image = to_image_out(record, thumbnail_url="https://signed.example.test/thumb")

    assert image.analysis == AnalysisOut(
        status=AnalysisStatus.FAILED,
        tags=[],
        description=None,
        colors=[],
        error="provider timed out",
    )


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_accepts_the_deep_flag(client: TestClient) -> None:
    async def database_answers() -> bool:
        return True

    # The deep check queries the database, and this test must not depend on one.
    client.app.dependency_overrides[get_database_check] = lambda: database_answers

    response = client.get("/api/health?deep=1")

    assert response.status_code == 200


def test_q_and_color_together_return_422_in_the_error_shape(client: TestClient) -> None:
    response = client.get("/api/v1/images?q=a&color=%23ffffff")

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "validation_error",
            "message": "query: only one of q, similar_to, color may be set; got q, color",
        }
    }


@pytest.mark.parametrize("query", ["q=a", "similar_to=1", "color=%23ffffff"])
def test_a_single_filter_is_allowed(client: TestClient, query: str) -> None:
    response = client.get(f"/api/v1/images?{query}")

    assert response.status_code == 501


def test_invalid_parameter_returns_422_in_the_error_shape(client: TestClient) -> None:
    response = client.get("/api/v1/images?limit=51")

    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert "limit" in body["error"]["message"]


def test_openapi_exposes_the_schemas_the_web_types_need(client: TestClient) -> None:
    schemas = client.get("/api/openapi.json").json()["components"]["schemas"]

    assert {"ImageOut", "ImagePage", "AnalysisOut", "ErrorResponse", "ErrorDetail"} <= set(schemas)


def test_every_422_is_documented_as_the_error_shape(client: TestClient) -> None:
    spec = client.get("/api/openapi.json").json()

    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            media = operation["responses"]["422"]["content"]["application/json"]
            assert media["schema"] == {"$ref": "#/components/schemas/ErrorResponse"}, (method, path)
