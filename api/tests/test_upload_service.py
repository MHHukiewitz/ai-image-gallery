import asyncio
import hashlib
import logging
import random
import re
import threading
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Any
from uuid import UUID

import pytest
from PIL import Image
from PIL.ExifTags import GPS, IFD, Base

from gallery.domain.errors import InvalidImageError, NotFoundError, QuotaExceededError
from gallery.domain.metadata import strip_metadata
from gallery.domain.models import AnalysisStatus, ImageRecord, NewImage
from gallery.domain.thumbnails import make_thumbnail
from gallery.services.uploads import UploadResult, UploadService, _shared_processing_slots

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
        uploaded_at=datetime(2026, 10, 20, tzinfo=UTC),
        status=AnalysisStatus.PENDING,
        tags=(),
        description=None,
        colors=(),
        error=None,
    )


class InMemoryImageRepo:
    """Records inserts and answers like the real repo: the new row is pending.

    `existing` holds the images the user already has, by hash. Inserted rows are deliberately not
    added to it, so tests that upload the same bytes many times don't dedupe against themselves.
    """

    def __init__(self) -> None:
        self.inserted: list[NewImage] = []
        self.fail_with: Exception | None = None
        self.existing: dict[str, ImageRecord] = {}
        self.lookups: list[list[str]] = []
        self.bumped: list[int] = []
        self.bumped_batches: list[list[int]] = []
        # Set when the image disappears between the lookup and the bump.
        self.bump_finds_nothing = False
        # The fake's clock. Each insert is stamped with it, and the quota counts those stamps.
        self.now = datetime(2026, 10, 20, 12, tzinfo=UTC)
        self.uploaded_at: list[datetime] = []
        self.count_calls: list[datetime] = []
        self.fail_count: Exception | None = None

    async def insert(self, image: NewImage) -> ImageRecord:
        if self.fail_with is not None:
            raise self.fail_with
        self.inserted.append(image)
        self.uploaded_at.append(self.now)
        return make_record(len(self.inserted), image)

    async def count_uploaded_since(self, since: datetime) -> int:
        self.count_calls.append(since)
        if self.fail_count is not None:
            raise self.fail_count
        return sum(1 for uploaded in self.uploaded_at if uploaded >= since)

    async def find_by_sha256(self, sha256s: Sequence[str]) -> dict[str, ImageRecord]:
        self.lookups.append(list(sha256s))
        return {sha: self.existing[sha] for sha in sha256s if sha in self.existing}

    async def bump(self, image_id: int) -> ImageRecord | None:
        self.bumped.append(image_id)
        if self.bump_finds_nothing:
            return None
        return next((r for r in self.existing.values() if r.id == image_id), None)

    async def bump_many(self, image_ids: Sequence[int]) -> list[ImageRecord]:
        self.bumped_batches.append(list(image_ids))
        by_id = {r.id: r for r in self.existing.values()}
        return [by_id[image_id] for image_id in image_ids if image_id in by_id]


@pytest.fixture(autouse=True)
def fresh_shared_processing_slots() -> Iterator[None]:
    """Each test runs in its own event loop, and the shared cap belongs to the loop that waits."""
    _shared_processing_slots.cache_clear()
    yield
    _shared_processing_slots.cache_clear()


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


def noise(image_format: str) -> bytes:
    """Noise compresses badly, so cutting the file in half cuts through real pixel data."""
    rng = random.Random(0)
    return encode(Image.frombytes("RGB", (64, 48), rng.randbytes(64 * 48 * 3)), image_format)


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

    record = (await service.upload(USER_ID, "logo.png", data)).record

    assert record.original_path.endswith("/original.png")
    assert store.objects[record.original_path] == (data, "image/png")
    assert record.mime_type == "image/png"


async def test_both_objects_share_one_folder_under_the_users_id(
    service: UploadService,
) -> None:
    record = (await service.upload(USER_ID, "beach.jpg", a_jpeg())).record

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
        record = (await service.upload(USER_ID, "an awkward ../name (1).jpg", data)).record

        assert pattern.match(record.original_path)
        assert pattern.match(record.thumbnail_path)


async def test_the_clients_filename_never_reaches_the_storage_path(
    service: UploadService,
) -> None:
    record = (await service.upload(USER_ID, "../../secret name.jpg", a_jpeg())).record

    assert "secret" not in record.original_path
    assert "secret" not in record.thumbnail_path


async def test_every_new_image_gets_its_own_folder(service: UploadService) -> None:
    first = (await service.upload(USER_ID, "a.jpg", a_jpeg())).record
    second = (await service.upload(USER_ID, "b.png", a_png())).record

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
    result = await service.upload(USER_ID, "beach.jpg", a_jpeg())

    record = result.record
    assert result.created is True
    assert record.status is AnalysisStatus.PENDING
    assert store.signed == [([record.thumbnail_path], SIGNED_URL_TTL)]
    assert (
        result.thumbnail_url == f"https://storage.test/{record.thumbnail_path}?ttl={SIGNED_URL_TTL}"
    )


async def test_a_signing_failure_after_the_insert_still_returns_the_saved_image(
    service: UploadService,
    repo: InMemoryImageRepo,
    store: InMemoryObjectStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store.fail_signing = RuntimeError("storage is saturated")

    with caplog.at_level(logging.WARNING):
        result = await service.upload(USER_ID, "beach.jpg", a_jpeg())

    record = result.record
    assert result.thumbnail_url == ""
    assert result.created is True
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
            stored_sha256=record.content_sha256,
        )
    ]
    # The image is saved, so nothing is cleaned up: both files stay for the row to point at.
    assert store.deleted == []
    assert set(store.objects) == {record.original_path, record.thumbnail_path}
    [warning] = [r for r in caplog.records if r.name == "gallery.services.uploads"]
    assert warning.levelno == logging.WARNING
    assert warning.thumbnail_paths == [record.thumbnail_path]


async def test_a_thumbnail_the_store_leaves_unsigned_gives_an_empty_url(
    service: UploadService, store: InMemoryObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def sign_nothing(paths: Sequence[str], ttl_seconds: int) -> dict[str, str]:
        return {}

    monkeypatch.setattr(store, "signed_urls", sign_nothing)

    result = await service.upload(USER_ID, "beach.jpg", a_jpeg())

    assert result.thumbnail_url == ""


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


async def test_an_insert_the_database_refuses_for_the_quota_removes_both_objects(
    service: UploadService, repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    # The quota check passed, then the database refused the row: two uploads raced for one slot.
    refusal = QuotaExceededError("You've reached today's upload limit.", retry_after_seconds=86400)
    repo.fail_with = refusal

    with pytest.raises(QuotaExceededError) as excinfo:
        await service.upload(USER_ID, "beach.jpg", a_jpeg())

    assert excinfo.value is refusal
    assert store.objects == {}
    [deleted] = store.deleted
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


# --- metadata ------------------------------------------------------------------------------


def a_phone_photo(image_format: str = "JPEG", make: str = "TestCam") -> bytes:
    """A photo with its position and camera in EXIF, turned a quarter turn, as phones write it.

    The pixels are always the same, and Pillow encodes them the same way, so two calls with
    another `make` give the same picture with other metadata.
    """
    exif = Image.Exif()
    exif[Base.Orientation] = 6
    exif[Base.Make] = make
    exif[IFD.GPSInfo] = {GPS.GPSLatitudeRef: "N", GPS.GPSLatitude: (52.0, 31.0, 12.0)}
    return encode(Image.new("RGB", (640, 480), "teal"), image_format, exif=exif)


@pytest.mark.parametrize(
    ("image_format", "mime_type"), [("JPEG", "image/jpeg"), ("PNG", "image/png")]
)
async def test_the_stored_original_has_no_gps_and_keeps_its_orientation(
    service: UploadService,
    store: InMemoryObjectStore,
    repo: InMemoryImageRepo,
    image_format: str,
    mime_type: str,
) -> None:
    data = a_phone_photo(image_format)

    record = (await service.upload(USER_ID, "beach.jpg", data)).record

    stored, content_type = store.objects[record.original_path]
    assert content_type == mime_type
    with Image.open(BytesIO(stored)) as original:
        exif = original.getexif()
        assert exif.get_ifd(IFD.GPSInfo) == {}
        assert dict(exif) == {Base.Orientation: 6}
    [new_image] = repo.inserted
    assert new_image.size_bytes == len(stored) < len(data)
    # The hash is still the uploaded file's, which is what the web client hashes for duplicates.
    assert new_image.content_sha256 == hashlib.sha256(data).hexdigest()
    assert (new_image.width, new_image.height) == (480, 640)


async def test_a_re_upload_of_the_same_photo_still_finds_the_stripped_one(
    service: UploadService, store: InMemoryObjectStore, repo: InMemoryImageRepo
) -> None:
    data = a_phone_photo()
    repo.existing[hashlib.sha256(data).hexdigest()] = an_existing_image(data)

    result = await service.upload(USER_ID, "beach.jpg", data)

    assert result.created is False
    assert store.objects == {}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def test_a_new_photo_is_inserted_with_the_hashes_of_the_upload_and_of_the_stored_file(
    service: UploadService, store: InMemoryObjectStore, repo: InMemoryImageRepo
) -> None:
    data = a_phone_photo()

    record = (await service.upload(USER_ID, "beach.jpg", data)).record

    stored, _ = store.objects[record.original_path]
    [new_image] = repo.inserted
    assert new_image.content_sha256 == sha256_hex(data)
    assert new_image.stored_sha256 == sha256_hex(stored) != new_image.content_sha256
    # One lookup asks for both.
    assert repo.lookups == [[sha256_hex(data), sha256_hex(stored)]]


async def test_the_same_picture_with_other_metadata_is_a_duplicate(
    service: UploadService, store: InMemoryObjectStore, repo: InMemoryImageRepo
) -> None:
    first = a_phone_photo(make="TestCam")
    retagged = a_phone_photo(make="OtherCam")
    assert sha256_hex(retagged) != sha256_hex(first)
    existing = an_existing_image(first)
    # The repo matches the stored hash of the first upload.
    repo.existing[sha256_hex(strip_metadata(first, "image/jpeg"))] = existing

    result = await service.upload(USER_ID, "retagged.jpg", retagged)

    assert result.created is False
    assert result.record.id == existing.id
    assert repo.bumped == [existing.id]
    assert store.objects == {}
    assert repo.inserted == []


async def test_a_downloaded_original_uploaded_again_is_a_duplicate(
    service: UploadService, store: InMemoryObjectStore, repo: InMemoryImageRepo
) -> None:
    first = a_phone_photo()
    downloaded = strip_metadata(first, "image/jpeg")
    existing = an_existing_image(first)
    repo.existing[sha256_hex(downloaded)] = existing

    result = await service.upload(USER_ID, "downloaded.jpg", downloaded)

    assert result.created is False
    assert result.record.id == existing.id
    assert store.objects == {}
    # Stripping a stripped file changes nothing, so the upload's own hash is the stored hash, and
    # the lookup asks for it once.
    assert repo.lookups == [[sha256_hex(downloaded)]]


async def test_an_original_the_stripper_cannot_parse_is_refused_and_nothing_is_stored(
    service: UploadService,
    store: InMemoryObjectStore,
    repo: InMemoryImageRepo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(data: bytes, mime_type: str) -> bytes:
        raise InvalidImageError("corrupt", "The image can't be decoded.")

    monkeypatch.setattr("gallery.services.uploads.strip_metadata", refuse)

    with pytest.raises(InvalidImageError):
        await service.upload(USER_ID, "beach.jpg", a_phone_photo())

    assert store.objects == {}
    assert repo.inserted == []


def an_existing_image(data: bytes, image_id: int = 7) -> ImageRecord:
    """The record of an image the user already has, whose bytes are `data`."""
    folder = f"{USER_ID}/existing-{image_id}"
    return make_record(
        image_id,
        NewImage(
            user_id=USER_ID,
            filename="first-upload.jpg",
            original_path=f"{folder}/original.jpg",
            thumbnail_path=f"{folder}/thumb.webp",
            mime_type="image/jpeg",
            size_bytes=len(data),
            width=640,
            height=480,
            content_sha256=hashlib.sha256(data).hexdigest(),
            stored_sha256=hashlib.sha256(data).hexdigest(),
        ),
    )


# --- duplicates ----------------------------------------------------------------------------


async def test_a_new_image_looks_for_its_own_hash_and_is_created(
    service: UploadService, repo: InMemoryImageRepo
) -> None:
    data = a_jpeg()

    result = await service.upload(USER_ID, "beach.jpg", data)

    assert isinstance(result, UploadResult)
    assert result.created is True
    assert len(repo.inserted) == 1
    assert repo.bumped == []
    # The hash is computed from the bytes. The service has no way to take one from the client.
    assert repo.lookups == [[hashlib.sha256(data).hexdigest()]]


async def test_the_lookup_happens_before_anything_is_written_to_storage(
    service: UploadService,
    repo: InMemoryImageRepo,
    store: InMemoryObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookups_seen_by_put: list[int] = []
    real_put = store.put

    async def put(path: str, data: bytes, content_type: str) -> None:
        lookups_seen_by_put.append(len(repo.lookups))
        await real_put(path, data, content_type)

    monkeypatch.setattr(store, "put", put)

    await service.upload(USER_ID, "beach.jpg", a_jpeg())

    assert lookups_seen_by_put == [1, 1]


async def test_a_duplicate_is_bumped_and_returned_without_a_storage_write(
    service: UploadService, repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    data = a_jpeg()
    existing = an_existing_image(data)
    repo.existing[existing.content_sha256] = existing

    result = await service.upload(USER_ID, "renamed-copy.jpg", data)

    assert result.created is False
    assert result.record == existing
    assert result.thumbnail_url == (
        f"https://storage.test/{existing.thumbnail_path}?ttl={SIGNED_URL_TTL}"
    )
    assert repo.lookups == [[existing.content_sha256]]
    assert repo.bumped == [existing.id]
    assert repo.inserted == []
    # Nothing was written, and so nothing needed cleaning up.
    assert store.objects == {}
    assert store.deleted == []
    assert store.signed == [([existing.thumbnail_path], SIGNED_URL_TTL)]


async def test_only_an_image_with_the_same_bytes_counts_as_a_duplicate(
    service: UploadService, repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    other = an_existing_image(a_png())
    repo.existing[other.content_sha256] = other

    result = await service.upload(USER_ID, "beach.jpg", a_jpeg())

    assert result.created is True
    assert result.record.id != other.id
    assert repo.bumped == []
    assert len(store.objects) == 2


async def test_a_duplicate_whose_thumbnail_cannot_be_signed_is_still_returned(
    service: UploadService, repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    data = a_jpeg()
    existing = an_existing_image(data)
    repo.existing[existing.content_sha256] = existing
    store.fail_signing = RuntimeError("storage is saturated")

    result = await service.upload(USER_ID, "beach.jpg", data)

    assert (result.created, result.thumbnail_url) == (False, "")
    assert result.record == existing


async def test_an_image_deleted_before_its_bump_is_stored_as_a_new_one(
    service: UploadService, repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    data = a_jpeg()
    existing = an_existing_image(data)
    repo.existing[existing.content_sha256] = existing
    repo.bump_finds_nothing = True

    result = await service.upload(USER_ID, "beach.jpg", data)

    assert repo.bumped == [existing.id]
    assert result.created is True
    assert len(repo.inserted) == 1
    assert len(store.objects) == 2


async def test_a_failed_lookup_stores_nothing(
    service: UploadService,
    repo: InMemoryImageRepo,
    store: InMemoryObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(sha256s: Sequence[str]) -> dict[str, ImageRecord]:
        raise ConnectionError("database is down")

    monkeypatch.setattr(repo, "find_by_sha256", broken)

    with pytest.raises(ConnectionError):
        await service.upload(USER_ID, "beach.jpg", a_jpeg())

    assert store.objects == {}
    assert repo.inserted == []


async def test_bump_returns_the_moved_image_with_a_signed_thumbnail(
    service: UploadService, repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    existing = an_existing_image(a_jpeg())
    repo.existing[existing.content_sha256] = existing

    result = await service.bump(existing.id)

    assert result == UploadResult(
        existing,
        f"https://storage.test/{existing.thumbnail_path}?ttl={SIGNED_URL_TTL}",
        created=False,
    )
    assert repo.bumped == [existing.id]
    assert store.objects == {}


async def test_bumping_a_missing_or_foreign_image_raises_not_found(
    service: UploadService, repo: InMemoryImageRepo
) -> None:
    with pytest.raises(NotFoundError, match="Image not found"):
        await service.bump(404)

    assert repo.bumped == [404]


async def test_bump_many_returns_the_moved_images_in_order_with_one_signing_call(
    service: UploadService, repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    first = an_existing_image(a_jpeg(), image_id=3)
    second = an_existing_image(a_png(), image_id=9)
    repo.existing = {first.content_sha256: first, second.content_sha256: second}

    results = await service.bump_many([9, 3])

    assert results == [
        UploadResult(
            second,
            f"https://storage.test/{second.thumbnail_path}?ttl={SIGNED_URL_TTL}",
            created=False,
        ),
        UploadResult(
            first,
            f"https://storage.test/{first.thumbnail_path}?ttl={SIGNED_URL_TTL}",
            created=False,
        ),
    ]
    assert repo.bumped_batches == [[9, 3]]
    # Every thumbnail is signed in a single Storage call.
    assert store.signed == [([second.thumbnail_path, first.thumbnail_path], SIGNED_URL_TTL)]
    assert store.objects == {}


async def test_bump_many_leaves_out_images_that_are_missing_or_not_the_callers(
    service: UploadService, repo: InMemoryImageRepo
) -> None:
    mine = an_existing_image(a_jpeg(), image_id=3)
    repo.existing = {mine.content_sha256: mine}

    results = await service.bump_many([404, 3, 405])

    assert [result.record.id for result in results] == [3]


async def test_bump_many_of_nothing_that_exists_signs_nothing(
    service: UploadService, store: InMemoryObjectStore
) -> None:
    assert await service.bump_many([404]) == []
    assert store.signed == []


async def test_bump_many_survives_a_signing_failure_with_empty_urls(
    service: UploadService,
    repo: InMemoryImageRepo,
    store: InMemoryObjectStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The images moved, so the request succeeds; the grid refreshes empty URLs on its next poll.
    existing = an_existing_image(a_jpeg(), image_id=3)
    repo.existing = {existing.content_sha256: existing}
    store.fail_signing = ConnectionError("storage is saturated")

    with caplog.at_level(logging.WARNING):
        results = await service.bump_many([3])

    assert [(result.record.id, result.thumbnail_url) for result in results] == [(3, "")]
    assert [r.levelno for r in caplog.records if r.name == "gallery.services.uploads"] == [
        logging.WARNING
    ]


async def test_find_duplicates_maps_hashes_to_ids_and_leaves_out_the_rest(
    service: UploadService, repo: InMemoryImageRepo
) -> None:
    owned = an_existing_image(a_jpeg(), image_id=7)
    repo.existing[owned.content_sha256] = owned
    unknown = "f" * 64

    matches = await service.find_duplicates([owned.content_sha256, unknown])

    assert matches == {owned.content_sha256: 7}


async def test_find_duplicates_asks_the_repo_once_per_distinct_hash(
    service: UploadService, repo: InMemoryImageRepo
) -> None:
    await service.find_duplicates(["a" * 64, "b" * 64, "a" * 64])

    assert repo.lookups == [["a" * 64, "b" * 64]]


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"plain text pretending to be a picture", "unsupported_type"),
        (encode(Image.new("RGB", (8, 8)), "GIF"), "unsupported_type"),
        (encode(Image.new("RGB", (8, 8)), "WEBP"), "unsupported_type"),
        (b"\0" * (MAX_BYTES + 1), "too_large"),
        (encode(Image.new("RGB", (1001, 1000)), "PNG"), "too_many_pixels"),
        (a_jpeg()[:200], "corrupt"),
        # Cut after the header, so only decoding the pixels can tell.
        (noise("JPEG")[: len(noise("JPEG")) // 2], "corrupt"),
        (noise("PNG")[: len(noise("PNG")) // 2], "corrupt"),
    ],
    ids=[
        "text",
        "gif",
        "webp",
        "too-large",
        "too-many-pixels",
        "truncated",
        "jpeg-cut-in-half",
        "png-cut-in-half",
    ],
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
    assert repo.lookups == []


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


class DecodeProbe:
    """Stands in for make_thumbnail and records how many calls run at the same moment."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = 0
        self.most_running = 0

    def __call__(self, data: bytes) -> bytes:
        with self._lock:
            self.running += 1
            self.most_running = max(self.most_running, self.running)
        try:
            # Long enough for every other upload to reach the cap while this one is "decoding".
            time.sleep(0.05)
            return make_thumbnail(data)
        finally:
            with self._lock:
                self.running -= 1


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> DecodeProbe:
    probe = DecodeProbe()
    monkeypatch.setattr("gallery.services.uploads.make_thumbnail", probe)
    return probe


async def test_no_more_uploads_are_decoded_at_once_than_the_semaphore_allows(
    repo: InMemoryImageRepo, store: InMemoryObjectStore, probe: DecodeProbe
) -> None:
    service = UploadService(
        repo,
        store,
        max_bytes=MAX_BYTES,
        max_pixels=MAX_PIXELS,
        signed_url_ttl=SIGNED_URL_TTL,
        processing_slots=asyncio.Semaphore(2),
    )

    await asyncio.gather(*(service.upload(USER_ID, "beach.jpg", a_jpeg()) for _ in range(8)))

    # Capped at 2, and not lower: the cap bounds the work without serializing it.
    assert probe.most_running == 2
    assert len(repo.inserted) == 8


async def test_the_cap_is_shared_by_every_service_and_comes_from_the_settings(
    repo: InMemoryImageRepo,
    store: InMemoryObjectStore,
    probe: DecodeProbe,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UPLOAD_PROCESSING_CONCURRENCY", "3")

    def per_request_service() -> UploadService:
        # The upload route builds a new service for every request.
        return UploadService(
            repo,
            store,
            max_bytes=MAX_BYTES,
            max_pixels=MAX_PIXELS,
            signed_url_ttl=SIGNED_URL_TTL,
        )

    await asyncio.gather(
        *(per_request_service().upload(USER_ID, "beach.jpg", a_jpeg()) for _ in range(9))
    )

    assert probe.most_running == 3


async def test_two_is_the_default_cap(
    repo: InMemoryImageRepo, store: InMemoryObjectStore, probe: DecodeProbe
) -> None:
    service = UploadService(
        repo, store, max_bytes=MAX_BYTES, max_pixels=MAX_PIXELS, signed_url_ttl=SIGNED_URL_TTL
    )

    await asyncio.gather(*(service.upload(USER_ID, "beach.jpg", a_jpeg()) for _ in range(6)))

    assert probe.most_running == 2


async def test_a_rejected_upload_gives_its_slot_back(
    repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    service = UploadService(
        repo,
        store,
        max_bytes=MAX_BYTES,
        max_pixels=MAX_PIXELS,
        signed_url_ttl=SIGNED_URL_TTL,
        processing_slots=asyncio.Semaphore(1),
    )

    with pytest.raises(InvalidImageError):
        await service.upload(USER_ID, "notes.txt", b"not an image")

    # Would wait forever if the failed upload still held the only slot.
    await asyncio.wait_for(service.upload(USER_ID, "beach.jpg", a_jpeg()), timeout=5)
    assert len(repo.inserted) == 1


# --- daily quota ---------------------------------------------------------------------------

QUOTA = 300
OTHER_USER_ID = UUID("3f1c9a7e-5b2d-4c8e-9a6f-0e1d2c3b4a59")


def quota_service(
    repo: InMemoryImageRepo,
    store: InMemoryObjectStore,
    *,
    daily_quota: int = QUOTA,
    exempt: frozenset[UUID] = frozenset(),
) -> UploadService:
    return UploadService(
        repo,
        store,
        max_bytes=MAX_BYTES,
        max_pixels=MAX_PIXELS,
        signed_url_ttl=SIGNED_URL_TTL,
        daily_quota=daily_quota,
        quota_exempt_user_ids=exempt,
        clock=lambda: repo.now,
    )


def tiny_jpeg() -> bytes:
    """Small, so 300 uploads in one test stay quick."""
    return encode(Image.new("RGB", (8, 8), "teal"), "JPEG")


async def test_the_300th_upload_in_a_day_passes_and_the_301st_is_refused(
    repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    service = quota_service(repo, store)
    data = tiny_jpeg()
    repo.uploaded_at = [repo.now] * (QUOTA - 1)
    # The 300th upload is a real one: it is stored and counted like any other.
    await service.upload(USER_ID, "beach.jpg", data)
    stored_before_refusal = dict(store.objects)

    with pytest.raises(QuotaExceededError) as refused:
        await service.upload(USER_ID, "beach.jpg", data)

    assert len(repo.inserted) == 1
    assert len(repo.uploaded_at) == QUOTA
    # The refusal comes before any Storage write, so it leaves no objects and no row behind.
    assert store.objects == stored_before_refusal
    assert refused.value.message == (
        "You've reached today's upload limit of 300 images. Try again tomorrow."
    )
    assert refused.value.retry_after_seconds == 24 * 60 * 60


async def test_the_quota_is_counted_over_the_last_24_hours_from_now(
    repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    service = quota_service(repo, store)
    repo.uploaded_at = [repo.now - timedelta(hours=23, minutes=59)] * QUOTA

    with pytest.raises(QuotaExceededError):
        await service.upload(USER_ID, "beach.jpg", tiny_jpeg())

    assert repo.count_calls == [repo.now - timedelta(hours=24)]


async def test_uploads_older_than_24_hours_do_not_count(
    repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    service = quota_service(repo, store)
    repo.uploaded_at = [repo.now - timedelta(hours=25)] * QUOTA

    result = await service.upload(USER_ID, "beach.jpg", tiny_jpeg())

    assert result.created


async def test_a_slot_frees_up_once_its_upload_is_24_hours_old(
    repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    service = quota_service(repo, store)
    repo.uploaded_at = [repo.now] * QUOTA
    with pytest.raises(QuotaExceededError):
        await service.upload(USER_ID, "beach.jpg", tiny_jpeg())

    repo.now += timedelta(hours=24, seconds=1)

    assert (await service.upload(USER_ID, "beach.jpg", tiny_jpeg())).created


async def test_a_duplicate_is_bumped_at_the_quota_and_never_counts(
    repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    service = quota_service(repo, store)
    data = tiny_jpeg()
    repo.existing[hashlib.sha256(data).hexdigest()] = an_existing_image(data)
    repo.uploaded_at = [repo.now] * (QUOTA - 1)

    bumped = await service.upload(USER_ID, "beach.jpg", data)

    assert bumped.created is False
    # The bump is answered before the quota is looked at, and it adds no upload.
    assert repo.count_calls == []
    assert len(repo.uploaded_at) == QUOTA - 1
    # That leaves the 300th slot for a new image.
    assert (await service.upload(USER_ID, "logo.png", a_png())).created
    # Now at the quota, a duplicate still bumps, and a new image is refused.
    assert (await service.upload(USER_ID, "beach.jpg", data)).created is False
    with pytest.raises(QuotaExceededError):
        await service.upload(USER_ID, "noise.png", noise("PNG"))


async def test_an_exempt_user_is_never_limited(
    repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    service = quota_service(repo, store, exempt=frozenset({OTHER_USER_ID, USER_ID}))
    repo.uploaded_at = [repo.now] * (QUOTA * 2)

    result = await service.upload(USER_ID, "beach.jpg", tiny_jpeg())

    assert result.created
    assert repo.count_calls == []


async def test_a_user_who_is_not_on_the_exempt_list_is_limited(
    repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    service = quota_service(repo, store, exempt=frozenset({OTHER_USER_ID}))
    repo.uploaded_at = [repo.now] * QUOTA

    with pytest.raises(QuotaExceededError):
        await service.upload(USER_ID, "beach.jpg", tiny_jpeg())


async def test_a_quota_of_zero_means_unlimited_and_never_asks_the_database(
    repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    service = quota_service(repo, store, daily_quota=0)
    repo.uploaded_at = [repo.now] * 5000

    result = await service.upload(USER_ID, "beach.jpg", tiny_jpeg())

    assert result.created
    assert repo.count_calls == []


async def test_a_quota_of_one_says_image_in_the_singular(
    repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    service = quota_service(repo, store, daily_quota=1)
    repo.uploaded_at = [repo.now]

    with pytest.raises(QuotaExceededError) as refused:
        await service.upload(USER_ID, "beach.jpg", tiny_jpeg())

    assert refused.value.message == (
        "You've reached today's upload limit of 1 image. Try again tomorrow."
    )


async def test_a_count_that_fails_refuses_the_upload_and_stores_nothing(
    repo: InMemoryImageRepo, store: InMemoryObjectStore
) -> None:
    service = quota_service(repo, store)
    repo.fail_count = ConnectionError("database is down")

    with pytest.raises(ConnectionError):
        await service.upload(USER_ID, "beach.jpg", tiny_jpeg())

    assert store.objects == {}
    assert repo.inserted == []
