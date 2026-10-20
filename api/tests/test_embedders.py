"""The fastembed adapter and the factory, with fastembed's model stubbed out.

The real model is exercised by tests/integration/test_embedding_model.py.
"""

import threading
from collections.abc import Iterable
from typing import Any

import numpy as np
import pytest
from fastembed import TextEmbedding

from gallery.adapters.embeddings import (
    DATABASE_DIMENSIONS,
    FakeEmbedder,
    FastEmbedEmbedder,
    build_embedder,
)
from gallery.config import Settings

MODEL = "BAAI/bge-small-en-v1.5"


class ModelStub:
    """Stands in for fastembed's model: records how it is built and used, and returns vectors."""

    def __init__(self) -> None:
        self.constructed: list[dict[str, Any]] = []
        self.embedded: list[list[str]] = []
        self.embed_threads: list[threading.Thread] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = self

        def init(this: TextEmbedding, model_name: str, cache_dir: str, threads: int) -> None:
            stub.constructed.append(
                {"model_name": model_name, "cache_dir": cache_dir, "threads": threads}
            )

        def embed(this: TextEmbedding, documents: list[str]) -> Iterable[np.ndarray]:
            stub.embedded.append(list(documents))
            stub.embed_threads.append(threading.current_thread())
            return (np.full(384, 0.05, dtype=np.float32) for _ in documents)

        monkeypatch.setattr(TextEmbedding, "__init__", init)
        monkeypatch.setattr(TextEmbedding, "embed", embed)


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> ModelStub:
    stub = ModelStub()
    stub.install(monkeypatch)
    return stub


def make_embedder(cache_dir: str = "/models") -> FastEmbedEmbedder:
    return FastEmbedEmbedder(model=MODEL, cache_dir=cache_dir)


def test_it_reports_the_model_and_its_dimensions_without_loading_the_model(
    model: ModelStub,
) -> None:
    embedder = make_embedder()

    assert embedder.model_id == MODEL
    assert embedder.dimensions == 384
    assert model.constructed == []


async def test_the_model_loads_on_first_use_with_one_thread_and_the_cache_dir(
    model: ModelStub,
) -> None:
    embedder = make_embedder("/opt/models")

    await embedder.embed(["a dog"])

    assert model.constructed == [{"model_name": MODEL, "cache_dir": "/opt/models", "threads": 1}]


async def test_the_model_loads_once_per_process(model: ModelStub) -> None:
    embedder = make_embedder()

    await embedder.embed(["one"])
    await embedder.embed(["two", "three"])

    assert len(model.constructed) == 1
    assert model.embedded == [["one"], ["two", "three"]]


async def test_it_returns_one_list_of_floats_per_text_in_order(model: ModelStub) -> None:
    vectors = await make_embedder().embed(["a", "b"])

    assert len(vectors) == 2
    assert all(len(vector) == 384 for vector in vectors)
    assert all(isinstance(value, float) for vector in vectors for value in vector)


async def test_embedding_nothing_does_not_load_the_model(model: ModelStub) -> None:
    assert await make_embedder().embed([]) == []
    assert model.constructed == []


async def test_the_model_runs_off_the_event_loop(model: ModelStub) -> None:
    await make_embedder().embed(["a dog"])

    assert model.embed_threads[0] is not threading.main_thread()


def test_an_unsupported_model_is_rejected() -> None:
    with pytest.raises(ValueError, match="does not support"):
        FastEmbedEmbedder(model="not/a-real-model", cache_dir="/models")


def test_the_factory_returns_nothing_when_embeddings_are_off(settings: Settings) -> None:
    assert settings.EMBEDDER == "none"
    assert build_embedder(settings) is None


def test_the_factory_builds_the_fastembed_embedder(
    monkeypatch: pytest.MonkeyPatch, model: ModelStub
) -> None:
    monkeypatch.setenv("EMBEDDER", "fastembed")
    monkeypatch.setenv("EMBEDDING_CACHE_DIR", "/somewhere")

    embedder = build_embedder(Settings())

    assert isinstance(embedder, FastEmbedEmbedder)
    assert embedder.model_id == MODEL
    assert embedder.dimensions == DATABASE_DIMENSIONS
    assert model.constructed == []


def test_the_factory_rejects_a_model_whose_vectors_do_not_fit_the_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMBEDDER", "fastembed")
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")

    with pytest.raises(ValueError, match="768-dimensional.*384"):
        build_embedder(Settings())


def test_the_factory_rejects_an_unknown_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDER", "fastembed")
    monkeypatch.setenv("EMBEDDING_MODEL", "nobody/nothing")

    with pytest.raises(ValueError, match="does not support"):
        build_embedder(Settings())


def test_the_fake_embedder_fits_the_column() -> None:
    assert FakeEmbedder().dimensions == DATABASE_DIMENSIONS
