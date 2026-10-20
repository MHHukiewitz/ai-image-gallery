"""SupabaseImageRepo's lookup by hash, bump and gallery order, against a client that records calls.

The SQL behind them (RLS scoping, the RPC's ownership check) is covered by the pgTAP tests and the
integration test; these check what the adapter asks PostgREST for and how it reads the answer.
"""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from gallery.adapters.supabase_client import AsyncClient
from gallery.adapters.supabase_images import SupabaseImageRepo
from gallery.domain.models import AllImages

OWNER_ID = "7b9e2c4a-1d3f-4e5a-8b6c-9d0e1f2a3b4c"


def detail_row(image_id: int, sha256: str) -> dict[str, Any]:
    """One `image_details` row, including the sort column the view now carries."""
    folder = f"{OWNER_ID}/{image_id}"
    return {
        "id": image_id,
        "user_id": OWNER_ID,
        "filename": f"photo-{image_id}.jpg",
        "original_path": f"{folder}/original.jpg",
        "thumbnail_path": f"{folder}/thumb.webp",
        "mime_type": "image/jpeg",
        "size_bytes": 1234,
        "width": 640,
        "height": 480,
        "content_sha256": sha256,
        "uploaded_at": "2026-10-20T12:00:00+00:00",
        "description": None,
        "tags": [],
        "colors": [],
        "ai_processing_status": "pending",
        "last_error": None,
        "last_uploaded_at": "2026-10-21T08:00:00+00:00",
    }


class FakePostgrest:
    """Answers every query on a table with its rows and every RPC with `rpc_data`.

    Query-builder calls (`select`, `in_`, `order` ...) are recorded in order, so a test can see
    exactly what was asked for.
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None, rpc_data: Any = None) -> None:
        self.rows = rows or []
        self.rpc_data = rpc_data
        self.queries: list[tuple[str, list[tuple[str, tuple[Any, ...], dict[str, Any]]]]] = []
        self.rpcs: list[tuple[str, dict[str, Any]]] = []

    def table(self, name: str) -> "FakeQuery":
        return FakeQuery(self, name)

    def rpc(self, name: str, params: dict[str, Any]) -> "FakeRpc":
        self.rpcs.append((name, params))
        return FakeRpc(self.rpc_data)


class FakeQuery:
    def __init__(self, client: FakePostgrest, table: str) -> None:
        self._client = client
        self._table = table
        self._calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, method: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> "FakeQuery":
            self._calls.append((method, args, kwargs))
            return self

        return record

    async def execute(self) -> SimpleNamespace:
        self._client.queries.append((self._table, self._calls))
        return SimpleNamespace(data=self._client.rows)


class FakeRpc:
    def __init__(self, data: Any) -> None:
        self._data = data

    async def execute(self) -> SimpleNamespace:
        return SimpleNamespace(data=self._data)


def repo_over(client: FakePostgrest) -> SupabaseImageRepo:
    return SupabaseImageRepo(cast(AsyncClient, client))


# --- find_by_sha256 ------------------------------------------------------------------------


async def test_find_by_sha256_asks_image_details_once_with_an_in_filter_newest_first() -> None:
    client = FakePostgrest([detail_row(2, "b" * 64)])

    await repo_over(client).find_by_sha256(["a" * 64, "b" * 64])

    assert client.queries == [
        (
            "image_details",
            [
                ("select", ("*",), {}),
                ("in_", ("content_sha256", ["a" * 64, "b" * 64]), {}),
                ("order", ("last_uploaded_at",), {"desc": True}),
                ("order", ("id",), {"desc": True}),
            ],
        )
    ]


async def test_find_by_sha256_maps_each_matching_hash_to_its_record() -> None:
    client = FakePostgrest([detail_row(2, "b" * 64), detail_row(1, "a" * 64)])

    matches = await repo_over(client).find_by_sha256(["a" * 64, "b" * 64, "c" * 64])

    assert {sha: record.id for sha, record in matches.items()} == {"a" * 64: 1, "b" * 64: 2}
    assert matches["a" * 64].content_sha256 == "a" * 64


async def test_find_by_sha256_keeps_the_newest_of_several_images_with_one_hash() -> None:
    # The query is ordered newest upload first, so the first row of a hash is the one to keep.
    client = FakePostgrest([detail_row(9, "a" * 64), detail_row(3, "a" * 64)])

    matches = await repo_over(client).find_by_sha256(["a" * 64])

    assert [record.id for record in matches.values()] == [9]


async def test_find_by_sha256_without_hashes_asks_nothing() -> None:
    client = FakePostgrest([detail_row(1, "a" * 64)])

    assert await repo_over(client).find_by_sha256([]) == {}
    assert client.queries == []


# --- bump ----------------------------------------------------------------------------------


async def test_bump_calls_the_rpc_then_reads_the_image_back() -> None:
    client = FakePostgrest([detail_row(5, "a" * 64)], rpc_data=True)

    record = await repo_over(client).bump(5)

    assert client.rpcs == [("bump_image", {"p_image_id": 5})]
    assert record is not None
    assert record.id == 5
    [(table, calls)] = client.queries
    assert table == "image_details"
    assert ("eq", ("id", 5), {}) in calls


@pytest.mark.parametrize("answer", [False, None, "true", 1, []], ids=repr)
async def test_bump_returns_none_unless_the_rpc_answers_exactly_true(answer: Any) -> None:
    client = FakePostgrest([detail_row(5, "a" * 64)], rpc_data=answer)

    assert await repo_over(client).bump(5) is None
    # Nothing to read back when nothing was bumped.
    assert client.queries == []


async def test_bump_returns_none_when_the_image_cannot_be_read_back() -> None:
    # image_details hides an image without metadata, so a true answer can still read as missing.
    client = FakePostgrest([], rpc_data=True)

    assert await repo_over(client).bump(5) is None


# --- the gallery order ---------------------------------------------------------------------


async def test_the_unfiltered_gallery_is_ordered_by_last_upload_then_id() -> None:
    client = FakePostgrest([detail_row(2, "b" * 64), detail_row(1, "a" * 64)])

    page = await repo_over(client).list(AllImages(), limit=20, offset=0)

    assert [image.id for image in page.items] == [2, 1]
    [(table, calls)] = client.queries
    assert table == "image_details"
    assert [call for call in calls if call[0] == "order"] == [
        ("order", ("last_uploaded_at",), {"desc": True}),
        ("order", ("id",), {"desc": True}),
    ]
