"""The API's query embedder: built lazily, once, in a thread, and wired into GET /api/v1/images.

No model is loaded: `build_embedder` is replaced by a spy that returns a FakeEmbedder.
"""

import asyncio
import threading
from collections.abc import Iterator
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from gallery.adapters.embeddings import DATABASE_DIMENSIONS, FakeEmbedder
from gallery.api import deps
from gallery.api.deps import CurrentUser
from gallery.config import Settings
from gallery.domain.models import Page, TextQuery
from gallery.ports import ImageRepo, ObjectStore, ShareRepo, TextEmbedder


class BuildSpy:
    """Stands in for `build_embedder`: counts the calls and says which thread made them."""

    def __init__(self) -> None:
        self.calls = 0
        self.threads: list[threading.Thread] = []
        self.error: Exception | None = None
        self.embedder = FakeEmbedder()

    def __call__(self, settings: Settings) -> TextEmbedder:
        self.calls += 1
        self.threads.append(threading.current_thread())
        if self.error is not None:
            raise self.error
        return self.embedder


@pytest.fixture(autouse=True)
def fresh_embedders() -> Iterator[None]:
    deps._query_embedders.clear()
    yield
    deps._query_embedders.clear()


@pytest.fixture
def build_spy(monkeypatch: pytest.MonkeyPatch) -> BuildSpy:
    spy = BuildSpy()
    monkeypatch.setattr(deps, "build_embedder", spy)
    return spy


def fastembed_settings(monkeypatch: pytest.MonkeyPatch, model: str | None = None) -> Settings:
    monkeypatch.setenv("EMBEDDER", "fastembed")
    if model is not None:
        monkeypatch.setenv("EMBEDDING_MODEL", model)
    return Settings()


# --- get_query_embedder --------------------------------------------------------------------


async def test_with_embedder_none_there_is_no_query_embedder(build_spy: BuildSpy) -> None:
    settings = Settings()
    assert settings.EMBEDDER == "none"

    assert await deps.get_query_embedder(settings) is None
    assert build_spy.calls == 0


async def test_with_fastembed_the_embedder_is_there_but_nothing_is_built_yet(
    build_spy: BuildSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedder = await deps.get_query_embedder(fastembed_settings(monkeypatch))

    assert embedder is not None
    assert embedder.model_id == "BAAI/bge-small-en-v1.5"
    assert embedder.dimensions == DATABASE_DIMENSIONS
    assert build_spy.calls == 0


async def test_every_request_gets_the_same_embedder_for_the_same_settings(
    build_spy: BuildSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = fastembed_settings(monkeypatch)

    first = await deps.get_query_embedder(settings)
    second = await deps.get_query_embedder(Settings())

    assert first is second


# --- LazyEmbedder --------------------------------------------------------------------------


async def test_the_first_embed_builds_the_embedder_in_a_thread_and_later_ones_reuse_it(
    build_spy: BuildSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedder = await deps.get_query_embedder(fastembed_settings(monkeypatch))
    assert embedder is not None

    first = await embedder.embed(["Tags: puppy"])
    second = await embedder.embed(["Tags: seaside"])

    assert [len(vector) for vector in first + second] == [DATABASE_DIMENSIONS] * 2
    assert build_spy.calls == 1
    assert build_spy.threads[0] is not threading.main_thread()
    assert build_spy.threads[0] is not threading.current_thread()


async def test_searches_that_arrive_together_build_the_embedder_once(
    build_spy: BuildSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedder = await deps.get_query_embedder(fastembed_settings(monkeypatch))
    assert embedder is not None

    await asyncio.gather(*(embedder.embed([f"Tags: query {i}"]) for i in range(8)))

    assert build_spy.calls == 1


async def test_a_failed_build_raises_and_the_next_embed_tries_again(
    build_spy: BuildSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedder = await deps.get_query_embedder(fastembed_settings(monkeypatch))
    assert embedder is not None
    build_spy.error = ValueError("fastembed does not support the embedding model 'x'")

    with pytest.raises(ValueError, match="does not support"):
        await embedder.embed(["Tags: puppy"])
    build_spy.error = None
    vectors = await embedder.embed(["Tags: puppy"])

    assert len(vectors) == 1
    assert build_spy.calls == 2


# --- GET /api/v1/images --------------------------------------------------------------------


@pytest.fixture
def repo() -> AsyncMock:
    mock = AsyncMock(spec=ImageRepo)
    mock.list.return_value = Page(items=[], next_offset=None)
    return mock


def client_for(repo: AsyncMock, embedder: TextEmbedder | None) -> TestClient:
    from gallery.main import create_app

    app = create_app()
    store = AsyncMock(spec=ObjectStore)
    store.signed_urls.return_value = {}
    user = CurrentUser(id=UUID(int=1), email="user@example.test", access_token="test-token")
    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[deps.get_image_repo] = lambda: repo
    app.dependency_overrides[deps.get_object_store] = lambda: store
    shares = AsyncMock(spec=ShareRepo)
    shares.list.return_value = []
    app.dependency_overrides[deps.get_share_repo] = lambda: shares
    app.dependency_overrides[deps.get_query_embedder] = lambda: embedder
    return TestClient(app, raise_server_exceptions=False)


def test_a_text_search_reaches_the_repo_with_its_embedding(repo: AsyncMock) -> None:
    client = client_for(repo, FakeEmbedder())

    response = client.get("/api/v1/images", params={"q": "puppy", "limit": 5, "offset": 10})

    assert response.status_code == 200
    (query,), kwargs = repo.list.await_args
    assert isinstance(query, TextQuery)
    assert query.q == "puppy"
    assert query.embedding is not None
    assert len(query.embedding) == DATABASE_DIMENSIONS
    assert kwargs == {"limit": 5, "offset": 10}


def test_a_text_search_without_an_embedder_reaches_the_repo_without_one(
    repo: AsyncMock,
) -> None:
    client = client_for(repo, None)

    response = client.get("/api/v1/images", params={"q": "puppy"})

    assert response.status_code == 200
    repo.list.assert_awaited_once_with(TextQuery("puppy"), limit=20, offset=0)


def test_an_embedder_that_fails_does_not_fail_the_search(repo: AsyncMock) -> None:
    failing = AsyncMock(spec=TextEmbedder)
    failing.embed.side_effect = RuntimeError("the model is gone")
    client = client_for(repo, failing)

    response = client.get("/api/v1/images", params={"q": "puppy"})

    assert response.status_code == 200
    repo.list.assert_awaited_once_with(TextQuery("puppy"), limit=20, offset=0)
