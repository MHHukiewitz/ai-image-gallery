"""Text search in GalleryService: the query is embedded, and any failure falls back to words only.

No network and no model: the embedder is a fake that records what it was asked, and the repo is
a mock that records the query it receives.
"""

import asyncio
import logging
from collections.abc import Sequence
from unittest.mock import AsyncMock

import pytest

from gallery.domain.models import AllImages, ColorQuery, GalleryQuery, Page, SimilarTo, TextQuery
from gallery.ports import ImageRepo, ObjectStore
from gallery.services import gallery as gallery_module
from gallery.services.gallery import GalleryService

SIGNED_URL_TTL = 900
VECTOR = [0.25, -0.5, 0.75]


class RecordingEmbedder:
    """Answers every text with the same vector and keeps the texts it was given."""

    model_id = "test:embedder:v1"
    dimensions = len(VECTOR)

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [list(VECTOR) for _ in texts]


class FailingEmbedder(RecordingEmbedder):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.texts.extend(texts)
        raise self._error


class StuckEmbedder(RecordingEmbedder):
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.texts.extend(texts)
        await asyncio.sleep(60)
        return []


class WrongCountEmbedder(RecordingEmbedder):
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return []


@pytest.fixture
def repo() -> AsyncMock:
    mock = AsyncMock(spec=ImageRepo)
    mock.list.return_value = Page(items=[], next_offset=None)
    return mock


@pytest.fixture
def store() -> AsyncMock:
    mock = AsyncMock(spec=ObjectStore)
    mock.signed_urls.return_value = {}
    return mock


def service_over(
    repo: AsyncMock, store: AsyncMock, embedder: RecordingEmbedder | None
) -> GalleryService:
    return GalleryService(repo, store, signed_url_ttl=SIGNED_URL_TTL, embedder=embedder)


# --- the embedding is passed to the repo ---------------------------------------------------


async def test_a_text_search_passes_the_query_embedding_to_the_repo(
    repo: AsyncMock, store: AsyncMock
) -> None:
    await service_over(repo, store, RecordingEmbedder()).list(
        TextQuery("puppy"), limit=20, offset=0
    )

    repo.list.assert_awaited_once_with(
        TextQuery("puppy", embedding=tuple(VECTOR)), limit=20, offset=0
    )


async def test_the_query_is_embedded_as_the_only_tag_of_an_image(
    repo: AsyncMock, store: AsyncMock
) -> None:
    # The stored vectors come from "{description} Tags: {tags}", and the minimum was calibrated
    # on queries embedded the same way, so a change here needs a new calibration.
    embedder = RecordingEmbedder()

    await service_over(repo, store, embedder).list(TextQuery("red car"), limit=20, offset=0)

    assert embedder.texts == ["Tags: red car"]


async def test_the_page_and_offset_are_the_repos_own(repo: AsyncMock, store: AsyncMock) -> None:
    repo.list.return_value = Page(items=[], next_offset=40)

    page, _ = await service_over(repo, store, RecordingEmbedder()).list(
        TextQuery("puppy"), limit=20, offset=20
    )

    assert page.next_offset == 40
    repo.list.assert_awaited_once_with(
        TextQuery("puppy", embedding=tuple(VECTOR)), limit=20, offset=20
    )


# --- no embedder, or a failing one: search by words only -----------------------------------


async def test_without_an_embedder_the_query_goes_through_as_it_is(
    repo: AsyncMock, store: AsyncMock
) -> None:
    # EMBEDDER=none: the API holds no embedder, and search is lexical-only as it was before.
    await service_over(repo, store, None).list(TextQuery("puppy"), limit=20, offset=0)

    repo.list.assert_awaited_once_with(TextQuery("puppy"), limit=20, offset=0)
    (query,), _ = repo.list.await_args
    assert query.embedding is None


@pytest.mark.parametrize(
    "embedder",
    [
        FailingEmbedder(RuntimeError("the model is gone")),
        FailingEmbedder(ValueError("EMBEDDING_MODEL is not a fastembed model")),
        WrongCountEmbedder(),
    ],
    ids=["runtime-error", "build-error", "no-vector"],
)
async def test_an_embedding_failure_falls_back_to_words_and_logs_a_warning(
    repo: AsyncMock,
    store: AsyncMock,
    embedder: RecordingEmbedder,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    page, _ = await service_over(repo, store, embedder).list(TextQuery("puppy"), limit=20, offset=0)

    assert page == Page(items=[], next_offset=None)
    repo.list.assert_awaited_once_with(TextQuery("puppy"), limit=20, offset=0)
    (record,) = [r for r in caplog.records if r.name == gallery_module.__name__]
    assert record.levelno == logging.WARNING


async def test_the_warning_does_not_hold_the_users_query(
    repo: AsyncMock, store: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    embedder = FailingEmbedder(RuntimeError("the model is gone"))

    await service_over(repo, store, embedder).list(
        TextQuery("my secret holiday"), limit=20, offset=0
    )

    assert "secret" not in caplog.text
    (record,) = [r for r in caplog.records if r.name == gallery_module.__name__]
    assert record.__dict__["query_length"] == len("my secret holiday")


async def test_a_stuck_embedder_times_out_and_the_search_goes_on_by_words(
    repo: AsyncMock, store: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gallery_module, "_EMBED_TIMEOUT_SECONDS", 0.01)

    await service_over(repo, store, StuckEmbedder()).list(TextQuery("puppy"), limit=20, offset=0)

    repo.list.assert_awaited_once_with(TextQuery("puppy"), limit=20, offset=0)


async def test_cancelling_the_search_is_not_swallowed(repo: AsyncMock, store: AsyncMock) -> None:
    # A client that hangs up cancels the request. The fallback must not turn that into a search.
    task = asyncio.create_task(
        service_over(repo, store, StuckEmbedder()).list(TextQuery("puppy"), limit=20, offset=0)
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    repo.list.assert_not_awaited()


# --- what is not embedded ------------------------------------------------------------------


async def test_a_blank_query_is_not_embedded(repo: AsyncMock, store: AsyncMock) -> None:
    embedder = RecordingEmbedder()

    await service_over(repo, store, embedder).list(TextQuery(""), limit=20, offset=0)

    assert embedder.texts == []
    repo.list.assert_awaited_once_with(TextQuery(""), limit=20, offset=0)


@pytest.mark.parametrize(
    "query",
    [AllImages(), SimilarTo(7), ColorQuery("#336699")],
    ids=["all", "similar", "color"],
)
async def test_the_other_search_modes_are_not_embedded(
    repo: AsyncMock, store: AsyncMock, query: GalleryQuery
) -> None:
    repo.get.return_value = object()  # a visible image, for the "similar to" guard
    embedder = RecordingEmbedder()

    await service_over(repo, store, embedder).list(query, limit=20, offset=0)

    assert embedder.texts == []
    repo.list.assert_awaited_once_with(query, limit=20, offset=0)
