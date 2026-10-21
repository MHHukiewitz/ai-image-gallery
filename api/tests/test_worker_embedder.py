"""How `worker.run` wires the embedder, with the database and the poll loop stubbed out."""

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

from gallery import worker
from gallery.adapters.embeddings import FastEmbedEmbedder
from gallery.config import Settings


class RecordingService:
    """Stands in for AnalysisService and remembers how the worker built it."""

    instances: list["RecordingService"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.kwargs = kwargs
        RecordingService.instances.append(self)

    async def process(self, job: object) -> None:
        raise NotImplementedError


@pytest.fixture
def connections(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace everything `run` reaches the network through, and note each attempt to connect."""
    attempts: list[str] = []
    RecordingService.instances = []

    async def connect(settings: Settings) -> object:
        attempts.append("connect")
        return MagicMock()

    async def close(client: object) -> None:
        return None

    async def no_loop(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(worker, "service_client", connect)
    monkeypatch.setattr(worker, "close_client", close)
    monkeypatch.setattr(worker, "work_loop", no_loop)
    monkeypatch.setattr(worker, "AnalysisService", RecordingService)
    # `run` lowers Pillow's pixel limit for the whole process, so give it back after the test.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", Image.MAX_IMAGE_PIXELS)
    return attempts


async def test_the_embedder_goes_to_the_service_when_it_is_switched_on(
    monkeypatch: pytest.MonkeyPatch, connections: list[str]
) -> None:
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-secret")
    monkeypatch.setenv("EMBEDDER", "fastembed")

    await worker.run(Settings(), asyncio.Event())

    (service,) = RecordingService.instances
    assert isinstance(service.kwargs["embedder"], FastEmbedEmbedder)


async def test_no_embedder_goes_to_the_service_by_default(
    monkeypatch: pytest.MonkeyPatch, connections: list[str]
) -> None:
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-secret")

    await worker.run(Settings(), asyncio.Event())

    (service,) = RecordingService.instances
    assert service.kwargs["embedder"] is None


async def test_a_misconfigured_embedder_stops_the_worker_before_it_connects(
    monkeypatch: pytest.MonkeyPatch, connections: list[str]
) -> None:
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-secret")
    monkeypatch.setenv("EMBEDDER", "fastembed")
    monkeypatch.setenv("EMBEDDING_MODEL", "nobody/nothing")

    with pytest.raises(ValueError, match="does not support"):
        await worker.run(Settings(), asyncio.Event())

    assert connections == []
