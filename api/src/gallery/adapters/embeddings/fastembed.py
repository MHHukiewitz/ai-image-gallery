"""TextEmbedder on fastembed: a small ONNX model that runs on the CPU, with no torch."""

import asyncio
import logging
import threading
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastembed import TextEmbedding

logger = logging.getLogger(__name__)


class FastEmbedEmbedder:
    """Embeds texts with a fastembed model, loaded on first use and then kept for the process.

    The model is single-threaded (`threads=1`) and used by one call at a time, so the worker's
    memory and CPU stay predictable next to its image decoding. `embed` runs in a thread, which
    keeps the event loop free.

    The import of fastembed (and with it onnxruntime) is deferred until it is needed, so a
    worker with `EMBEDDER=none` never pays for it.
    """

    def __init__(self, *, model: str, cache_dir: str) -> None:
        """Raises `ValueError` when fastembed doesn't support `model`."""
        from fastembed import TextEmbedding

        dimensions = {
            entry["model"]: entry["dim"] for entry in TextEmbedding.list_supported_models()
        }
        if model not in dimensions:
            raise ValueError(f"fastembed does not support the embedding model {model!r}")
        self._model_name = model
        self._cache_dir = cache_dir
        self._dimensions = dimensions[model]
        self._model: TextEmbedding | None = None
        self._lock = threading.Lock()

    @property
    def model_id(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        return await asyncio.to_thread(self._embed, list(texts))

    def _embed(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            model = self._loaded_model()
            return [vector.tolist() for vector in model.embed(texts)]

    def _loaded_model(self) -> "TextEmbedding":
        """Load the model once. Callers hold the lock."""
        if self._model is None:
            from fastembed import TextEmbedding

            started = time.monotonic()
            self._model = TextEmbedding(
                model_name=self._model_name, cache_dir=self._cache_dir, threads=1
            )
            logger.info(
                "embedding model loaded",
                extra={
                    "embedding_model": self._model_name,
                    "load_ms": round((time.monotonic() - started) * 1000),
                },
            )
        return self._model
