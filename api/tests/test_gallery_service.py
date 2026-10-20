"""GalleryService against in-memory fakes: no network and no Supabase."""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

import pytest

from gallery.domain.errors import NotFoundError
from gallery.domain.models import (
    AllImages,
    AnalysisStatus,
    GalleryQuery,
    ImageRecord,
    Page,
    TextQuery,
)
from gallery.services.gallery import GalleryService

OWNER_ID = UUID("7b9e2c4a-1d3f-4e5a-8b6c-9d0e1f2a3b4c")
SIGNED_URL_TTL = 900


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


class FakeImageRepo:
    """Keeps the rows in memory and answers like the adapter: a missing row is None or False."""

    def __init__(self, *records: ImageRecord) -> None:
        self.records = {record.id: record for record in records}
        self.queries: list[GalleryQuery] = []
        self.reanalysed: list[int] = []

    async def get(self, image_id: int) -> ImageRecord | None:
        return self.records.get(image_id)

    async def list(self, query: GalleryQuery, *, limit: int, offset: int) -> Page[ImageRecord]:
        self.queries.append(query)
        if not isinstance(query, AllImages):
            raise NotImplementedError(f"{type(query).__name__} is not implemented yet")
        newest_first = sorted(self.records.values(), key=lambda record: record.id, reverse=True)
        end = offset + limit
        return Page(
            items=newest_first[offset:end],
            next_offset=end if end < len(newest_first) else None,
        )

    async def delete(self, image_id: int) -> ImageRecord | None:
        return self.records.pop(image_id, None)

    async def request_reanalysis(self, image_id: int) -> bool:
        if image_id not in self.records:
            return False
        self.reanalysed.append(image_id)
        return True


class FakeObjectStore:
    """Holds object paths in memory and signs only the ones it holds, as the real store does."""

    def __init__(self, *paths: str) -> None:
        self.objects = set(paths)
        self.signed_calls: list[tuple[list[str], int]] = []
        self.delete_error: Exception | None = None

    async def signed_urls(self, paths: Sequence[str], ttl_seconds: int) -> dict[str, str]:
        self.signed_calls.append((list(paths), ttl_seconds))
        return {path: signed(path) for path in paths if path in self.objects}

    async def delete(self, paths: Sequence[str]) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.objects.difference_update(paths)


def store_holding(*records: ImageRecord) -> FakeObjectStore:
    """A store that holds both files of each record."""
    return FakeObjectStore(
        *(path for record in records for path in (record.original_path, record.thumbnail_path))
    )


# --- list ----------------------------------------------------------------------------------


async def test_list_signs_every_thumbnail_in_one_call() -> None:
    records = [make_record(1), make_record(2), make_record(3)]
    store = store_holding(*records)
    service = GalleryService(FakeImageRepo(*records), store, signed_url_ttl=SIGNED_URL_TTL)

    page, thumbnail_urls = await service.list(AllImages(), limit=20, offset=0)

    assert [record.id for record in page.items] == [3, 2, 1]
    assert store.signed_calls == [
        ([record.thumbnail_path for record in page.items], SIGNED_URL_TTL)
    ]
    assert thumbnail_urls == {
        record.thumbnail_path: signed(record.thumbnail_path) for record in records
    }


async def test_list_signs_only_the_thumbnails_of_its_page() -> None:
    records = [make_record(1), make_record(2), make_record(3)]
    store = store_holding(*records)
    service = GalleryService(FakeImageRepo(*records), store, signed_url_ttl=SIGNED_URL_TTL)

    page, thumbnail_urls = await service.list(AllImages(), limit=2, offset=0)

    assert [record.id for record in page.items] == [3, 2]
    assert page.next_offset == 2
    assert set(thumbnail_urls) == {records[2].thumbnail_path, records[1].thumbnail_path}


async def test_list_leaves_out_a_thumbnail_the_store_cannot_sign() -> None:
    records = [make_record(1), make_record(2)]
    # Record 2's files are gone from storage, so its thumbnail can't be signed.
    store = store_holding(records[0])
    service = GalleryService(FakeImageRepo(*records), store, signed_url_ttl=SIGNED_URL_TTL)

    page, thumbnail_urls = await service.list(AllImages(), limit=20, offset=0)

    assert len(page.items) == 2
    assert set(thumbnail_urls) == {records[0].thumbnail_path}


async def test_a_search_mode_fails_before_anything_is_signed() -> None:
    record = make_record(1)
    store = store_holding(record)
    service = GalleryService(FakeImageRepo(record), store, signed_url_ttl=SIGNED_URL_TTL)

    with pytest.raises(NotImplementedError):
        await service.list(TextQuery("beach"), limit=20, offset=0)

    assert store.signed_calls == []


# --- get -----------------------------------------------------------------------------------


async def test_get_returns_the_record_and_both_signed_urls() -> None:
    record = make_record(1)
    store = store_holding(record)
    service = GalleryService(FakeImageRepo(record), store, signed_url_ttl=SIGNED_URL_TTL)

    found, thumbnail_url, original_url = await service.get(1)

    assert found == record
    assert thumbnail_url == signed(record.thumbnail_path)
    assert original_url == signed(record.original_path)
    assert store.signed_calls == [([record.thumbnail_path, record.original_path], SIGNED_URL_TTL)]


async def test_get_on_a_missing_image_raises_not_found() -> None:
    service = GalleryService(FakeImageRepo(), FakeObjectStore(), signed_url_ttl=SIGNED_URL_TTL)

    with pytest.raises(NotFoundError):
        await service.get(1)


async def test_get_gives_an_empty_url_for_an_object_it_cannot_sign() -> None:
    record = make_record(1)
    store = FakeObjectStore(record.thumbnail_path)
    service = GalleryService(FakeImageRepo(record), store, signed_url_ttl=SIGNED_URL_TTL)

    _, thumbnail_url, original_url = await service.get(1)

    assert thumbnail_url == signed(record.thumbnail_path)
    assert original_url == ""


# --- delete --------------------------------------------------------------------------------


async def test_delete_removes_the_row_and_both_objects() -> None:
    record = make_record(1)
    repo = FakeImageRepo(record)
    store = store_holding(record)
    service = GalleryService(repo, store, signed_url_ttl=SIGNED_URL_TTL)

    await service.delete(1)

    assert await repo.get(1) is None
    assert store.objects == set()


async def test_delete_on_a_missing_image_raises_not_found_and_keeps_the_objects() -> None:
    record = make_record(1)
    store = store_holding(record)
    service = GalleryService(FakeImageRepo(), store, signed_url_ttl=SIGNED_URL_TTL)

    with pytest.raises(NotFoundError):
        await service.delete(1)

    assert store.objects == {record.original_path, record.thumbnail_path}


async def test_delete_succeeds_when_the_objects_cannot_be_removed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    record = make_record(1)
    repo = FakeImageRepo(record)
    store = store_holding(record)
    store.delete_error = RuntimeError("storage is down")
    service = GalleryService(repo, store, signed_url_ttl=SIGNED_URL_TTL)

    await service.delete(1)

    assert await repo.get(1) is None
    failure = next(entry for entry in caplog.records if entry.levelno == logging.ERROR)
    assert failure.name == "gallery.services.gallery"
    assert failure.image_id == 1


# --- reanalyze -----------------------------------------------------------------------------


async def test_reanalyze_queues_an_existing_image() -> None:
    repo = FakeImageRepo(make_record(1))
    service = GalleryService(repo, FakeObjectStore(), signed_url_ttl=SIGNED_URL_TTL)

    await service.reanalyze(1)

    assert repo.reanalysed == [1]


async def test_reanalyze_raises_not_found_when_the_repo_refuses() -> None:
    service = GalleryService(FakeImageRepo(), FakeObjectStore(), signed_url_ttl=SIGNED_URL_TTL)

    with pytest.raises(NotFoundError):
        await service.reanalyze(1)
