"""A deterministic TextEmbedder for tests and offline development. It loads no model."""

import hashlib
import math
import re
from collections.abc import Sequence

# Matches the width of `image_metadata.embedding`, so fake vectors can be stored locally.
DEFAULT_DIMENSIONS = 384
_WORD = re.compile(r"[a-z0-9]+")


class FakeEmbedder:
    """Embeds a text as a hashed bag of its words, so texts that share words score higher.

    Each word adds +1 or -1 to one dimension, chosen by a SHA-256 hash of the word (not `hash()`,
    which changes between processes). The vector is then scaled to unit length. A text without
    any word gets a fixed unit vector.
    """

    def __init__(self, dimensions: int = DEFAULT_DIMENSIONS) -> None:
        if dimensions < 1:
            raise ValueError("dimensions must be at least 1")
        self._dimensions = dimensions

    @property
    def model_id(self) -> str:
        return "fake:hashed-words:v1"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        for word in _WORD.findall(text.lower()):
            digest = hashlib.sha256(word.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self._dimensions
            vector[index] += 1.0 if digest[4] % 2 == 0 else -1.0
        length = math.sqrt(sum(value * value for value in vector))
        if length == 0:
            # No words, or words that cancelled out: a fixed direction keeps the vector valid.
            vector[0] = 1.0
            return vector
        return [value / length for value in vector]
