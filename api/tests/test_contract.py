"""Contract tests for the frozen API surface that the later tickets build on."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from gallery.api.schemas import AnalysisOut, ImageOut, to_image_out
from gallery.domain.models import AnalysisStatus, ImageRecord

OWNER_ID = UUID("7b9e2c4a-1d3f-4e5a-8b6c-9d0e1f2a3b4c")


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
