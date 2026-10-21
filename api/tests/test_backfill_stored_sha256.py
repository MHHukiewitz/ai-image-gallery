"""scripts/backfill_stored_sha256.py against in-memory images: what it counts, what it writes, that
a second run does nothing, that a failure doesn't stop it, and that it never logs a hash."""

import hashlib
import importlib.util
import logging
from pathlib import Path
from types import ModuleType

import pytest


def _load_script() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "backfill_stored_sha256.py"
    spec = importlib.util.spec_from_file_location("backfill_stored_sha256", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script()
UnhashedImage = script.UnhashedImage


class InMemoryImages:
    """Images by id, each with its stored bytes and its stored hash (None until it is set)."""

    def __init__(self, stored: dict[int, bytes], hashed: dict[int, str] | None = None) -> None:
        self.stored = stored
        self.hashes: dict[int, str | None] = {image_id: None for image_id in stored}
        self.hashes.update(hashed or {})
        self.broken: set[int] = set()
        self.downloads: list[int] = []

    async def page(self, after_image_id: int, limit: int) -> list[UnhashedImage]:
        ids = sorted(i for i, h in self.hashes.items() if h is None and i > after_image_id)
        return [UnhashedImage(i, f"owner/{i}/original.jpg") for i in ids[:limit]]

    async def download(self, image: UnhashedImage) -> bytes:
        self.downloads.append(image.image_id)
        if image.image_id in self.broken:
            raise RuntimeError("storage said no")
        return self.stored[image.image_id]

    async def record(self, image: UnhashedImage, stored_sha256: str) -> None:
        if self.hashes[image.image_id] is None:
            self.hashes[image.image_id] = stored_sha256


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def test_a_dry_run_counts_the_images_without_a_stored_hash_and_writes_nothing() -> None:
    images = InMemoryImages({1: b"one", 2: b"two", 3: b"three"}, hashed={2: sha256_hex(b"two")})

    counts = await script.backfill(images, apply=False)

    assert (counts.missing, counts.written, counts.failed) == (2, 0, 0)
    assert images.downloads == []
    assert images.hashes[1] is None


async def test_apply_sets_each_missing_hash_to_the_hash_of_the_stored_bytes() -> None:
    images = InMemoryImages({1: b"one", 2: b"two"}, hashed={2: sha256_hex(b"two")})

    counts = await script.backfill(images, apply=True)

    assert (counts.missing, counts.written, counts.failed) == (1, 1, 0)
    assert images.hashes == {1: sha256_hex(b"one"), 2: sha256_hex(b"two")}
    # The image that already had one was never downloaded.
    assert images.downloads == [1]


async def test_a_second_run_finds_nothing_left_to_do() -> None:
    images = InMemoryImages({1: b"one", 2: b"two"})
    await script.backfill(images, apply=True)

    counts = await script.backfill(images, apply=True)

    assert (counts.missing, counts.written, counts.failed) == (0, 0, 0)


async def test_a_failing_image_is_counted_and_the_run_goes_on_past_it() -> None:
    images = InMemoryImages({1: b"one", 2: b"two", 3: b"three"})
    images.broken = {2}

    counts = await script.backfill(images, apply=True, batch_size=1)

    assert (counts.missing, counts.written, counts.failed) == (3, 2, 1)
    assert images.hashes[2] is None
    assert images.hashes[3] == sha256_hex(b"three")


async def test_paging_reaches_every_image() -> None:
    images = InMemoryImages({i: f"image-{i}".encode() for i in range(1, 8)})

    counts = await script.backfill(images, apply=True, batch_size=3)

    assert counts.written == 7
    assert all(h is not None for h in images.hashes.values())


async def test_it_never_logs_a_hash(caplog: pytest.LogCaptureFixture) -> None:
    images = InMemoryImages({1: b"one"})

    with caplog.at_level(logging.DEBUG):
        await script.backfill(images, apply=True)

    logged = caplog.text + " ".join(str(record.__dict__) for record in caplog.records)
    assert sha256_hex(b"one") not in logged
