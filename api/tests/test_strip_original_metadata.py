"""scripts/strip_original_metadata.py against in-memory originals: what it counts, what it rewrites,
that a second run does nothing, and that it never logs a tag value."""

import importlib.util
import logging
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from PIL import ExifTags, Image

from gallery.domain.metadata import strip_metadata


def _load_script() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "strip_original_metadata.py"
    spec = importlib.util.spec_from_file_location("strip_original_metadata", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script()
StoredOriginal = script.StoredOriginal

MAKE = "TestCam-7Q"
SERIAL = "SN-31337"


def encode(image_format: str, **options: Any) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (64, 48), "teal").save(buffer, format=image_format, **options)
    return buffer.getvalue()


def exif(*, gps: bool) -> Image.Exif:
    tags = Image.Exif()
    tags[ExifTags.Base.Orientation] = 6
    tags[ExifTags.Base.Make] = MAKE
    tags[ExifTags.IFD.Exif] = {ExifTags.Base.BodySerialNumber: SERIAL}
    if gps:
        tags[ExifTags.IFD.GPSInfo] = {
            ExifTags.GPS.GPSLatitudeRef: "N",
            ExifTags.GPS.GPSLatitude: (52.0, 31.0, 12.0),
        }
    return tags


GPS_JPEG = encode("JPEG", exif=exif(gps=True))
GPS_PNG = encode("PNG", exif=exif(gps=True))
CAMERA_JPEG = encode("JPEG", exif=exif(gps=False))
COMMENTED_JPEG = encode("JPEG", comment="shot at home")
CLEAN_JPEG = encode("JPEG")
CLEAN_PNG = encode("PNG")


class InMemoryOriginals:
    """Rows and objects in dicts, like the bucket and the images table seen with the secret key."""

    def __init__(self, files: Sequence[tuple[bytes, str]]) -> None:
        self.rows: dict[int, StoredOriginal] = {}
        self.objects: dict[str, bytes] = {}
        for image_id, (data, mime_type) in enumerate(files, start=1):
            path = f"user/{image_id}/original"
            self.rows[image_id] = StoredOriginal(image_id, path, mime_type, len(data))
            self.objects[path] = data
        self.replaced: list[int] = []
        self.pages: list[tuple[int, int]] = []
        self.fail_replace_for: set[int] = set()
        # Images their owner deletes while the run is between its download and its write.
        self.deleted_meanwhile: set[int] = set()

    async def page(self, after_image_id: int, limit: int) -> list[StoredOriginal]:
        self.pages.append((after_image_id, limit))
        ids = sorted(image_id for image_id in self.rows if image_id > after_image_id)[:limit]
        return [self.rows[image_id] for image_id in ids]

    async def download(self, original: StoredOriginal) -> bytes:
        return self.objects[original.path]

    async def replace(self, original: StoredOriginal, data: bytes) -> bool:
        if original.image_id in self.fail_replace_for:
            raise ConnectionError("storage is down")
        if original.image_id in self.deleted_meanwhile:
            del self.rows[original.image_id]
            del self.objects[original.path]
            return False
        self.replaced.append(original.image_id)
        self.objects[original.path] = data
        return True

    async def record_size(self, original: StoredOriginal, size_bytes: int) -> None:
        self.rows[original.image_id] = StoredOriginal(
            original.image_id, original.path, original.mime_type, size_bytes
        )


EVERY_KIND = [
    (GPS_JPEG, "image/jpeg"),
    (GPS_PNG, "image/png"),
    (CAMERA_JPEG, "image/jpeg"),
    (COMMENTED_JPEG, "image/jpeg"),
    (CLEAN_JPEG, "image/jpeg"),
    (CLEAN_PNG, "image/png"),
    (b"not an image", "image/jpeg"),
]


async def test_a_dry_run_counts_each_kind_and_writes_nothing() -> None:
    originals = InMemoryOriginals(EVERY_KIND)
    objects_before = dict(originals.objects)
    rows_before = dict(originals.rows)

    counts = await script.strip_originals(originals, apply=False)

    assert counts == script.StripCounts(
        scanned=7, gps=2, other_exif=1, other_metadata=1, clean=2, failed=1
    )
    assert originals.objects == objects_before
    assert originals.rows == rows_before


async def test_apply_strips_what_carries_metadata_and_records_the_new_sizes() -> None:
    originals = InMemoryOriginals(EVERY_KIND)

    counts = await script.strip_originals(originals, apply=True)

    assert (counts.rewritten, counts.resized, counts.failed) == (4, 4, 1)
    assert sorted(originals.replaced) == [1, 2, 3, 4]
    for image_id, (data, mime_type) in enumerate(EVERY_KIND[:6], start=1):
        row = originals.rows[image_id]
        stored = originals.objects[row.path]
        assert stored == strip_metadata(data, mime_type)
        assert row.size_bytes == len(stored)
    with Image.open(BytesIO(originals.objects[originals.rows[1].path])) as image:
        assert image.getexif().get_ifd(ExifTags.IFD.GPSInfo) == {}
        assert dict(image.getexif()) == {ExifTags.Base.Orientation: 6}


async def test_a_second_run_finds_nothing_left_to_do() -> None:
    originals = InMemoryOriginals(EVERY_KIND[:6])
    await script.strip_originals(originals, apply=True)
    originals.replaced.clear()

    counts = await script.strip_originals(originals, apply=True)

    assert counts == script.StripCounts(scanned=6, clean=6)
    assert originals.replaced == []


async def test_a_size_left_stale_by_an_interrupted_run_is_corrected() -> None:
    stripped = strip_metadata(GPS_JPEG, "image/jpeg")
    originals = InMemoryOriginals([(stripped, "image/jpeg")])
    # The object was overwritten, but the run stopped before it updated the row.
    originals.rows[1] = StoredOriginal(1, originals.rows[1].path, "image/jpeg", len(GPS_JPEG))

    counts = await script.strip_originals(originals, apply=True)

    assert (counts.clean, counts.rewritten, counts.resized) == (1, 0, 1)
    assert originals.rows[1].size_bytes == len(stripped)


async def test_a_failed_rewrite_is_counted_and_the_run_goes_on() -> None:
    originals = InMemoryOriginals([(GPS_JPEG, "image/jpeg"), (CAMERA_JPEG, "image/jpeg")])
    originals.fail_replace_for = {1}

    counts = await script.strip_originals(originals, apply=True)

    assert (counts.failed, counts.rewritten) == (1, 1)
    assert originals.replaced == [2]
    # The size isn't changed for an object that wasn't rewritten.
    assert originals.rows[1].size_bytes == len(GPS_JPEG)


async def test_an_image_deleted_during_the_run_is_left_deleted() -> None:
    originals = InMemoryOriginals([(GPS_JPEG, "image/jpeg"), (CAMERA_JPEG, "image/jpeg")])
    originals.deleted_meanwhile = {1}

    counts = await script.strip_originals(originals, apply=True)

    assert (counts.deleted, counts.rewritten, counts.resized, counts.failed) == (1, 1, 1, 0)
    assert 1 not in originals.rows
    assert originals.rows[2].size_bytes == len(originals.objects[originals.rows[2].path])


async def test_it_pages_forward_by_image_id() -> None:
    originals = InMemoryOriginals([(CLEAN_JPEG, "image/jpeg")] * 5)

    counts = await script.strip_originals(originals, apply=False, batch_size=2)

    assert counts.scanned == 5
    assert originals.pages == [(0, 2), (2, 2), (4, 2), (5, 2)]


async def test_no_tag_value_reaches_the_log(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    await script.strip_originals(InMemoryOriginals(EVERY_KIND), apply=True)

    logged = " ".join(str(record.__dict__) for record in caplog.records)
    assert caplog.records
    for value in (MAKE, SERIAL, "52.0", "31.0", "12.0", "shot at home"):
        assert value not in logged


def test_main_exits_1_when_an_original_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_run(settings: Any, **options: Any) -> Any:
        calls.append(options)
        return script.StripCounts(scanned=1, failed=1)

    monkeypatch.setattr(script, "run", fake_run)
    monkeypatch.setattr(script, "init_observability", lambda settings, component: None)
    user_id = "7b9e2c4a-1d3f-4e5a-8b6c-9d0e1f2a3b4c"

    with pytest.raises(SystemExit) as exited:
        script.main(["--apply", "--user-id", user_id])

    assert exited.value.code == 1
    assert [(options["apply"], str(options["user_id"])) for options in calls] == [(True, user_id)]


def test_main_defaults_to_a_dry_run_over_every_user(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_run(settings: Any, **options: Any) -> Any:
        calls.append(options)
        return script.StripCounts(scanned=3, clean=3)

    monkeypatch.setattr(script, "run", fake_run)
    monkeypatch.setattr(script, "init_observability", lambda settings, component: None)

    with pytest.raises(SystemExit) as exited:
        script.main([])

    assert exited.value.code == 0
    assert calls == [{"apply": False, "user_id": None}]
