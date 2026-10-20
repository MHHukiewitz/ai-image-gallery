"""The search modes of the Supabase image repo, and the "similar to" guard in the service.

No network: the RPC client is a fake that records each call and answers with the rows it was
given. The rows have the shape the search functions return: the `image_details` columns plus
`score`.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from gallery.adapters.supabase_client import AsyncClient
from gallery.adapters.supabase_images import SupabaseImageRepo
from gallery.domain.errors import NotFoundError
from gallery.domain.models import (
    AnalysisStatus,
    ColorQuery,
    GalleryQuery,
    ImageRecord,
    Page,
    SimilarTo,
    TextQuery,
)
from gallery.ports import ImageRepo, ObjectStore
from gallery.services.gallery import GalleryService

OWNER_ID = UUID("7b9e2c4a-1d3f-4e5a-8b6c-9d0e1f2a3b4c")
SIGNED_URL_TTL = 900


def search_row(image_id: int, *, score: float = 0.5) -> dict[str, Any]:
    """One row as a search function returns it: the `image_details` columns, then `score`."""
    folder = f"{OWNER_ID}/{image_id}"
    return {
        "id": image_id,
        "user_id": str(OWNER_ID),
        "filename": f"photo-{image_id}.jpg",
        "original_path": f"{folder}/original.jpg",
        "thumbnail_path": f"{folder}/thumb.webp",
        "mime_type": "image/jpeg",
        "size_bytes": 1234,
        "width": 640,
        "height": 480,
        "content_sha256": "a" * 64,
        "uploaded_at": "2026-10-20T12:00:00+00:00",
        "description": "A beach at dusk.",
        "tags": ["beach"],
        "colors": ["#336699"],
        "ai_processing_status": "done",
        "last_error": None,
        "score": score,
    }


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


class FakeRpcClient:
    """Answers every RPC with the same rows, and records each function name with its parameters."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def rpc(self, name: str, params: dict[str, Any]) -> "FakeRpc":
        return FakeRpc(self, name, params)


class FakeRpc:
    def __init__(self, client: FakeRpcClient, name: str, params: dict[str, Any]) -> None:
        self._client = client
        self._name = name
        self._params = params

    async def execute(self) -> SimpleNamespace:
        self._client.calls.append((self._name, self._params))
        return SimpleNamespace(data=self._client.rows)


def repo_over(client: FakeRpcClient) -> SupabaseImageRepo:
    return SupabaseImageRepo(cast(AsyncClient, client))


# --- the RPC each query calls --------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected_call"),
    [
        (TextQuery("beach"), ("search_images", {"q": "beach", "lim": 3, "off": 4})),
        (
            TextQuery("beach", embedding=(0.25, -0.5)),
            (
                "search_images_semantic",
                {"q": "beach", "q_embedding": [0.25, -0.5], "lim": 3, "off": 4},
            ),
        ),
        (SimilarTo(7), ("similar_images", {"target_id": 7, "lim": 3, "off": 4})),
        (ColorQuery("#336699"), ("images_by_color", {"hex": "#336699", "lim": 3, "off": 4})),
    ],
    ids=["text", "text-with-embedding", "similar", "color"],
)
async def test_each_search_mode_calls_its_rpc_for_one_row_more_than_the_page(
    query: GalleryQuery, expected_call: tuple[str, dict[str, Any]]
) -> None:
    client = FakeRpcClient()

    await repo_over(client).list(query, limit=2, offset=4)

    assert client.calls == [expected_call]


# --- pages and scores ----------------------------------------------------------------------


async def test_a_full_page_with_an_extra_row_has_a_next_offset() -> None:
    client = FakeRpcClient([search_row(3), search_row(2), search_row(1)])

    page = await repo_over(client).list(TextQuery("beach"), limit=2, offset=4)

    assert [item.id for item in page.items] == [3, 2]
    assert page.next_offset == 6


async def test_a_semantic_text_search_pages_like_any_other() -> None:
    client = FakeRpcClient([search_row(3), search_row(2), search_row(1)])

    page = await repo_over(client).list(TextQuery("puppy", embedding=(1.0, 0.0)), limit=2, offset=2)

    assert [item.id for item in page.items] == [3, 2]
    assert page.next_offset == 4


async def test_the_last_page_has_no_next_offset() -> None:
    client = FakeRpcClient([search_row(2), search_row(1)])

    page = await repo_over(client).list(TextQuery("beach"), limit=2, offset=0)

    assert [item.id for item in page.items] == [2, 1]
    assert page.next_offset is None


async def test_a_search_with_no_match_is_an_empty_page() -> None:
    page = await repo_over(FakeRpcClient()).list(ColorQuery("#336699"), limit=20, offset=0)

    assert page == Page(items=[], next_offset=None)


async def test_each_result_carries_its_score_and_its_metadata() -> None:
    client = FakeRpcClient([search_row(1, score=0.875)])

    page = await repo_over(client).list(SimilarTo(9), limit=20, offset=0)

    (item,) = page.items
    assert item.score == 0.875
    assert item.status is AnalysisStatus.DONE
    assert item.tags == ("beach",)
    assert item.colors == ("#336699",)


async def test_results_keep_the_rank_the_function_gave_them() -> None:
    # Ids descending would be the table's order. The rank puts 1 first, so nothing re-sorts them.
    client = FakeRpcClient([search_row(1, score=0.9), search_row(5, score=0.4)])

    page = await repo_over(client).list(TextQuery("beach"), limit=20, offset=0)

    assert [item.id for item in page.items] == [1, 5]
    assert [item.score for item in page.items] == [0.9, 0.4]


# --- the "similar to" guard in the service -------------------------------------------------


async def test_similar_to_a_missing_or_foreign_image_is_not_found() -> None:
    # RLS hides another user's image the same way it hides a missing one, so both read as None.
    repo = AsyncMock(spec=ImageRepo)
    repo.get.return_value = None
    store = AsyncMock(spec=ObjectStore)
    service = GalleryService(repo, store, signed_url_ttl=SIGNED_URL_TTL)

    with pytest.raises(NotFoundError):
        await service.list(SimilarTo(7), limit=20, offset=0)

    repo.get.assert_awaited_once_with(7)
    repo.list.assert_not_awaited()
    store.signed_urls.assert_not_awaited()


async def test_similar_to_a_visible_image_searches_and_signs_its_page() -> None:
    repo = AsyncMock(spec=ImageRepo)
    repo.get.return_value = make_record(1)
    neighbour = make_record(2)
    repo.list.return_value = Page(items=[neighbour], next_offset=None)
    store = AsyncMock(spec=ObjectStore)
    store.signed_urls.return_value = {neighbour.thumbnail_path: "https://signed.example.test/t"}
    service = GalleryService(repo, store, signed_url_ttl=SIGNED_URL_TTL)

    page, thumbnail_urls = await service.list(SimilarTo(1), limit=20, offset=0)

    repo.get.assert_awaited_once_with(1)
    repo.list.assert_awaited_once_with(SimilarTo(1), limit=20, offset=0)
    assert page.items == [neighbour]
    assert thumbnail_urls == {neighbour.thumbnail_path: "https://signed.example.test/t"}


@pytest.mark.parametrize(
    "query", [TextQuery("beach"), ColorQuery("#336699")], ids=["text", "color"]
)
async def test_text_and_color_searches_do_not_look_up_a_target(query: GalleryQuery) -> None:
    repo = AsyncMock(spec=ImageRepo)
    repo.list.return_value = Page(items=[], next_offset=None)
    service = GalleryService(repo, AsyncMock(spec=ObjectStore), signed_url_ttl=SIGNED_URL_TTL)

    await service.list(query, limit=20, offset=0)

    repo.get.assert_not_awaited()
    repo.list.assert_awaited_once_with(query, limit=20, offset=0)
