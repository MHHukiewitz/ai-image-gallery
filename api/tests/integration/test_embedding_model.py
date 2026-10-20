"""The real embedding model, against the calibration behind `search_embedding_floor()`.

These tests load BAAI/bge-small-en-v1.5 through fastembed, so they skip unless the model is already
in `EMBEDDING_CACHE_DIR`. The Docker image has it in /opt/models. To run them locally, point the
variable at a writable folder and let fastembed download the model into it once (about 65 MB):

    cd api
    export EMBEDDING_CACHE_DIR=$HOME/.cache/gallery-models
    uv run python -c "from fastembed import TextEmbedding; \\
      TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='$EMBEDDING_CACHE_DIR')"
    uv run pytest -m integration tests/integration/test_embedding_model.py

The floor is read from the migration, so changing it there without recalibrating fails these tests.
"""

import math
import os
import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from gallery.adapters.embeddings import DATABASE_DIMENSIONS, FastEmbedEmbedder
from gallery.domain.embedding_text import embedding_text

MODEL = "BAAI/bge-small-en-v1.5"
MIGRATION = Path(__file__).parents[3] / "supabase/migrations/20261020000008_image_embeddings.sql"

# conftest.py clears the settings variables before every test, so read the cache dir now, while
# the tests are being collected.
_CACHE_DIR = os.environ.get("EMBEDDING_CACHE_DIR")


def _model_is_cached() -> bool:
    return bool(_CACHE_DIR) and any(Path(_CACHE_DIR or "").glob("models--*bge-small-en-v1.5*"))


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _model_is_cached(),
        reason=f"{MODEL} is not in EMBEDDING_CACHE_DIR (see this module's docstring)",
    ),
]


@pytest.fixture(scope="module")
def embedder() -> FastEmbedEmbedder:
    assert _CACHE_DIR is not None
    return FastEmbedEmbedder(model=MODEL, cache_dir=_CACHE_DIR)


def floor() -> float:
    sql = MIGRATION.read_text()
    match = re.search(
        r"search_embedding_floor\(\) returns float8\s+language sql [^\n]*\s+return ([0-9.]+);", sql
    )
    assert match, "the migration no longer defines search_embedding_floor() as a literal"
    return float(match.group(1))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


async def semantic_score(embedder: FastEmbedEmbedder, a: str, b: str) -> float:
    """What `similar_images` makes of two texts: the cosine, floored and stretched to 0..1."""
    vector_a, vector_b = await embedder.embed([a, b])
    return max(0.0, (cosine(vector_a, vector_b) - floor()) / (1 - floor()))


async def test_it_makes_unit_vectors_that_fit_the_database_column(
    embedder: FastEmbedEmbedder,
) -> None:
    vectors = await embedder.embed(["a dog in a park", "a red house"])

    assert embedder.dimensions == DATABASE_DIMENSIONS
    for vector in vectors:
        assert len(vector) == DATABASE_DIMENSIONS
        assert math.isclose(math.sqrt(sum(value * value for value in vector)), 1.0, abs_tol=1e-3)


async def test_the_same_text_gives_the_same_vector(embedder: FastEmbedEmbedder) -> None:
    first, second = (
        await embedder.embed(["a puppy on the grass"]),
        await embedder.embed(["a puppy on the grass"]),
    )

    assert first == second


async def test_a_text_embeds_the_same_alone_or_in_a_batch(embedder: FastEmbedEmbedder) -> None:
    (alone,) = await embedder.embed(["a puppy on the grass"])
    batched, _ = await embedder.embed(["a puppy on the grass", "an ocean at dusk, with long waves"])

    assert cosine(alone, batched) > 0.999


@pytest.mark.parametrize(
    ("a", "b", "at_least"),
    [("dog", "puppy", 0.4), ("beach", "sea", 0.4), ("house", "red house", 0.1)],
)
async def test_related_words_score_clearly_above_zero(
    embedder: FastEmbedEmbedder, a: str, b: str, at_least: float
) -> None:
    assert await semantic_score(embedder, a, b) >= at_least


@pytest.mark.parametrize(("a", "b"), [("dog", "car"), ("beach", "snow")])
async def test_unrelated_words_score_near_zero(embedder: FastEmbedEmbedder, a: str, b: str) -> None:
    assert await semantic_score(embedder, a, b) < 0.1


async def test_related_words_outscore_unrelated_ones(embedder: FastEmbedEmbedder) -> None:
    related = [
        await semantic_score(embedder, a, b) for a, b in [("dog", "puppy"), ("beach", "sea")]
    ]
    unrelated = [
        await semantic_score(embedder, a, b) for a, b in [("dog", "car"), ("beach", "snow")]
    ]

    assert min(related) > max(unrelated)


def _text(description: str, *tags: str) -> str:
    return embedding_text(tags, description)


async def test_whole_image_texts_separate_alike_images_from_different_ones(
    embedder: FastEmbedEmbedder,
) -> None:
    """The texts the worker really embeds: a description plus tags."""
    el_greco = _text(
        "An oil painting of Jesus in a blue cloak and red robe, wearing a crown of thorns and "
        "holding a wooden cross against a stormy sky.",
        "jesus", "cross", "crown of thorns", "painting", "el greco", "blue cloak", "red robe",
        "dramatic sky",
    )  # fmt: skip
    mosaic = _text(
        "A painting of Jesus with a crown of thorns and a halo, embracing a wooden cross in a "
        "red robe and blue cloak.",
        "jesus", "cross", "crown of thorns", "painting", "red robe", "blue cloak", "halo",
        "religious art",
    )  # fmt: skip
    beach = _text(
        "Waves rolling onto a sandy beach at sunset under an orange sky.",
        "beach", "sunset", "sea", "waves", "sand", "orange sky", "horizon", "coast",
    )  # fmt: skip
    sailboat = _text(
        "A white sailboat on a calm blue sea under a clear sky.",
        "sea", "boat", "ocean", "blue", "sailing", "water", "horizon",
    )  # fmt: skip
    pizza = _text(
        "A pizza with melted cheese and basil on a wooden table.",
        "pizza", "food", "cheese", "restaurant", "italian", "table",
    )  # fmt: skip
    car = _text(
        "A red sports car driving down a city street.",
        "car", "red", "sports car", "road", "vehicle", "speed", "city",
    )  # fmt: skip

    assert await semantic_score(embedder, el_greco, mosaic) > 0.6
    assert await semantic_score(embedder, beach, sailboat) > 0.2
    assert await semantic_score(embedder, pizza, car) == 0
    assert await semantic_score(embedder, pizza, beach) == 0
    assert await semantic_score(embedder, car, mosaic) == 0
