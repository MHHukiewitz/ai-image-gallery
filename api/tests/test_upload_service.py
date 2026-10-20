import hashlib
import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from io import BytesIO
from typing import Any
from uuid import UUID

import pytest
from PIL import Image
from PIL.ExifTags import Base

from gallery.domain.errors import InvalidImageError
from gallery.domain.models import AnalysisStatus, ImageRecord, NewImage
from gallery.services.uploads import UploadService

USER_ID = UUID("7b9e2c4a-1d3f-4e5a-8b6c-9d0e1f2a3b4c")
MAX_BYTES = 1024 * 1024
MAX_PIXELS = 1_000_000
SIGNED_URL_TTL = 900


class InMemoryObjectStore:
    """Keeps objects in a dict. Failures are switched on per path suffix."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.fail_put_suffix: str | None = None
        self.fail_delete = False
        self.fail_signing: Exception | None = None
        self.deleted: list[list[str]] = []
        self.signed: list[tuple[list[str], int]] = []

    async def put(self, path: str, data: bytes, content_type: str) -> None:
        if self.fail_put_suffix and path.endswith(self.fail_put_suffix):
            raise ConnectionError("storage is down")
        self.objects[path] = (data, content_type)

    async def get(self, path: str) -> bytes:
        return self.objects[path][0]

    async def delete(self, paths: Sequence[str]) -> None:
        self.deleted.append(list(paths))
        if self.fail_delete:
            raise ConnectionError("storage is down")
        for path in paths:
            self.objects.pop(path, None)

    async def signed_urls(self, paths: Sequence[str], ttl_seconds: int) -> dict[str, str]:
        self.signed.append((list(paths), ttl_seconds))
        if self.fail_signing is not None:
            raise self.fail_signing
        return {path: f"https://storage.test/{path}?ttl={ttl_seconds}" for path in paths}


class InMemoryImageRepo:
    """Records inserts and answers like the real repo: the new row is pending."""

    def __init__(self) -> None:
        self.inserted: list[NewImage] = []
        self.fail_with: Exception | None = None

    async def insert(self, image: NewImage) -> ImageRecord:
        if self.fail_with is not None:
            raise self.fail_with
        self.inserted.append(image)
        return ImageRecord(
            id=len(self.inserted),
            user_id=image.user_id,
            filename=image.filename,
            original_path=image.original_path,
            thumbnail_path=image.thumbnail_path,
            mime_type=image.mime_type,
            size_bytes=image.size_bytes,
            width=image.width,
            height=image.height,
            content_sha256=image.content_sha256,
            uploaded_at=datetime(2026, 10, 20, tzinfo=UTC),
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
def service(repo: InMemoryImageRepo, store: InMemoryObjectStore) -> UploadService:
    return UploadService(
        repo,
        store,
        max_bytes=MAX_BYTES,
        max_pixels=MAX_PIXELS,
        signed_url_ttl=SIGNED_URL_TTL,
    )


def encode(image: Image.Image, image_format: str, **options: Any) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format=image_format, **options)
    return buffer.getvalue()


def a_jpeg() -> bytes:
    return encode(Image.new("RGB", (640, 480), "teal"), "JPEG")


def a_png() -> bytes:
    return encode(Image.new("RGBA", (200, 100), "orange"), "PNG")


async def test_a_jpeg_is_stored_as_an_original_and_a_thumbnail(
    service: UploadService, store: InMemoryObjectStore
) -> None:
    data = a_jpeg()

    await service.upload(USER_ID, "beach.jpg", data)

    assert len(store.objects) == 2
    original_path = next(path for path in store.objects if "/original." in path)
    thumbnail_path = next(path for path in store.objects if path.endswith("/thumb.webp"))
    assert original_path.startswith(f"{USER_ID}/")
    assert thumbnail_path.startswith(f"{USER_ID}/")
    assert original_path.endswith("/original.jpg")
    assert store.objects[original_path] == (data, "image/jpeg")
    thumbnail_bytes, thumbnail_type = store.objects[thumbnail_path]
    assert thumbnail_type == "image/webp"
    thumbnail = Image.open(BytesIO(thumbnail_bytes))
    assert (thumbnail.format, thumbnail.size) == ("WEBP", (300, 300))


async def test_a_png_keeps_its_own_extension_and_type(
    service: UploadService, store: InMemoryObjectStore
) -> None:
    data = a_png()

    record, _ = await service.upload(USER_ID, "logo.png", data)

    assert record.original_path.endswith("/original.png")
    assert store.objects[record.original_path] == (data, "image/png")
    assert record.mime_type == "image/png"


async def test_both_objects_share_one_folder_under_the_users_id(
    service: UploadService,
) -> None:
    record, _ = await service.upload(USER_ID, "beach.jpg", a_jpeg())

    original_folder, _, _ = record.original_path.rpartition("/")
    thumbnail_folder, _, _ = record.thumbnail_path.rpartition("/")
    assert original_folder == thumbnail_folder
    user_part, _, key = original_folder.partition("/")
    assert user_part == str(USER_ID)
    assert UUID(key).version == 4


async def test_the_paths_satisfy_the_databases_owner_folder_constraint(
    service: UploadService,
) -> None:
    # The same pattern as the CHECK constraints in the schema migration. A key that breaks it
    # is rejected by the database after the objects were already stored.
    pattern = re.compile(rf"^{USER_ID}/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+\.[A-Za-z0-9]+$")

    for data in (a_jpeg(), a_png()):
        record, _ = await service.upload(USER_ID, "an awkward ../name (1).jpg", data)

        assert pattern.match(record.original_path)
        assert pattern.match(record.thumbnail_path)


async def test_the_clients_filename_never_reaches_the_storage_path(
    service: UploadService,
) -> None:
    record, _ = await service.upload(USER_ID, "../../secret name.jpg", a_jpeg())

    assert "secret" not in record.original_path
    assert "secret" not in record.thumbnail_path


async def test_every_upload_gets_its_own_folder(service: UploadService) -> None:
    data = a_jpeg()

    first, _ = await service.upload(USER_ID, "a.jpg", data)
    second, _ = await service.upload(USER_ID, "a.jpg", data)

    assert first.original_path != second.original_path


async def test_the_row_describes_the_original(
    service: UploadService, repo: InMemoryImageRepo
) -> None:
    data = a_jpeg()

    await service.upload(USER_ID, "beach.jpg", data)

    [new_image] = repo.inserted
    assert new_image.user_id == USER_ID
    assert new_image.filename == "beach.jpg"
    assert new_image.mime_type == "image/jpeg"
    assert new_image.size_bytes == len(data)
    assert (new_image.width, new_image.height) == (640, 480)
    assert new_image.content_sha256 == hashlib.sha256(data).hexdigest()
    assert new_image.original_path.endswith("/original.jpg")
    assert new_image.thumbnail_path.endswith("/thumb.webp")


async def test_the_row_reports_the_upright_size_of_a_rotated_photo(
    service: UploadService, repo: InMemoryImageRepo
) -> None:
    exif = Image.Exif()
    exif[Base.Orientation] = 6
    data = encode(Image.new("RGB", (640, 480)), "JPEG", exif=exif)

    await service.upload(USER_ID, "portrait.jpg", data)

    assert (repo.inserted[0].width, repo.inserted[0].height) == (480, 640)


async def test_the_filename_is_cleaned_before_it_is_stored(
    service: UploadService, repo: InMemoryImageRepo
) -> None:
    await service.upload(USER_ID, "C:\\Users\\Me\\..\\holiday\x00.jpg", a_jpeg())
    await service.upload(USER_ID, None, a_jpeg())

    assert [image.filename for image in repo.inserted] == ["holiday.jpg", "image"]


async def test_the_result_is_the_inserted_record_and_a_signed_thumbnail_url(
    service: UploadService, store: InMemoryObjectStore
) -> None:
    record, thumbnail_url = await service.upload(USER_ID, "beach.jpg", a_jpeg())

    assert record.status is AnalysisStatus.PENDING
    assert store.signed == [([record.thumbnail_path], SIGNED_URL_TTL)]
    assert thumbnail_url == f"https://storage.test/{record.thumbnail_path}?ttl={SIGNED_URL_TTL}"


async def test_a_signing_failure_after_the_insert_still_returns_the_saved_image(
    service: UploadService,
    repo: InMemoryImageRepo,
    store: InMemoryObjectStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store.fail_signing = RuntimeError("storage is saturated")

    with caplog.at_level(logging.WARNING):
        record, thumbnail_url = await service.upload(USER_ID, "beach.jpg", a_jpeg())

    assert thumbnail_url == ""
    assert repo.inserted == [
        NewImage(
            user_id=USER_ID,
            filename="beach.jpg",
            original_path=record.original_path,
            thumbnail_path=record.thumbnail_path,
            mime_type="image/jpeg",
            size_bytes=record.size_bytes,
            width=640,
            height=480,
            content_sha256=record.content_sha256,
        )
    ]
    # The image is saved, so nothing is cleaned up: both files stay for the row to point at.
    assert store.deleted == []
    assert set(store.objects) == {record.original_path, record.thumbnail_path}
    [warning] = [r for r in caplog.records if r.name == "gallery.services.uploads"]
    assert warning.levelno == logging.WARNING
    assert warning.thumbnail_path == record.thumbnail_path


async def test_a_thumbnail_the_store_leaves_unsigned_gives_an_empty_url(
    service: UploadService, store: InMemoryObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def sign_nothing(paths: Sequence[str], ttl_seconds: int) -> dict[str, str]:
        return {}

    monkeypatch.setattr(store, "signed_urls", sign_nothing)

    _, thumbnail_url = await service.upload(USER_ID, "beach.jpg", a_jpeg())

    assert thumbnail_url == ""


async def test_a_failed_insert_deletes_both_objects_and_raises_the_same_error(
    service: UploadService, repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    failure = RuntimeError("database is down")
    repo.fail_with = failure

    with pytest.raises(RuntimeError) as excinfo:
        await service.upload(USER_ID, "beach.jpg", a_jpeg())

    assert excinfo.value is failure
    assert store.objects == {}
    [deleted] = store.deleted
    assert len(deleted) == 2
    assert all(path.startswith(f"{USER_ID}/") for path in deleted)
    assert sorted(path.rpartition("/")[2] for path in deleted) == ["original.jpg", "thumb.webp"]


async def test_a_failed_thumbnail_upload_also_removes_the_original(
    service: UploadService, repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    store.fail_put_suffix = "/thumb.webp"

    with pytest.raises(ConnectionError):
        await service.upload(USER_ID, "beach.jpg", a_jpeg())

    assert store.objects == {}
    assert repo.inserted == []


async def test_a_failed_original_upload_stores_and_inserts_nothing(
    service: UploadService, repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    store.fail_put_suffix = "/original.jpg"

    with pytest.raises(ConnectionError):
        await service.upload(USER_ID, "beach.jpg", a_jpeg())

    assert store.objects == {}
    assert repo.inserted == []


async def test_a_failed_cleanup_does_not_hide_the_original_error(
    service: UploadService,
    repo: InMemoryImageRepo,
    store: InMemoryObjectStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo.fail_with = RuntimeError("database is down")
    store.fail_delete = True

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="database is down"):
        await service.upload(USER_ID, "beach.jpg", a_jpeg())

    [log_record] = [r for r in caplog.records if r.name == "gallery.services.uploads"]
    assert log_record.levelno == logging.ERROR
    assert sorted(log_record.paths) == sorted(store.deleted[0])


async def test_a_successful_upload_deletes_nothing(
    service: UploadService, store: InMemoryObjectStore
) -> None:
    await service.upload(USER_ID, "beach.jpg", a_jpeg())

    assert store.deleted == []


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"plain text pretending to be a picture", "unsupported_type"),
        (encode(Image.new("RGB", (8, 8)), "GIF"), "unsupported_type"),
        (encode(Image.new("RGB", (8, 8)), "WEBP"), "unsupported_type"),
        (b"\0" * (MAX_BYTES + 1), "too_large"),
        (encode(Image.new("RGB", (1001, 1000)), "PNG"), "too_many_pixels"),
        (a_jpeg()[:200], "corrupt"),
    ],
    ids=["text", "gif", "webp", "too-large", "too-many-pixels", "truncated"],
)
async def test_an_invalid_file_is_rejected_before_anything_is_stored(
    service: UploadService,
    repo: InMemoryImageRepo,
    store: InMemoryObjectStore,
    data: bytes,
    code: str,
) -> None:
    with pytest.raises(InvalidImageError) as excinfo:
        await service.upload(USER_ID, "photo.jpg", data)

    assert excinfo.value.code == code
    assert store.objects == {}
    assert store.deleted == []
    assert store.signed == []
    assert repo.inserted == []


async def test_the_limits_come_from_the_constructor(
    repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    data = a_jpeg()
    tight = UploadService(
        repo,
        store,
        max_bytes=len(data) - 1,
        max_pixels=MAX_PIXELS,
        signed_url_ttl=SIGNED_URL_TTL,
    )

    with pytest.raises(InvalidImageError) as excinfo:
        await tight.upload(USER_ID, "beach.jpg", data)

    assert excinfo.value.code == "too_large"
    assert tight.max_bytes == len(data) - 1
