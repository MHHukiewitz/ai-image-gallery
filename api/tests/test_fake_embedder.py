import asyncio
import json
import math
import subprocess
import sys
from collections.abc import Sequence

from gallery.adapters.embeddings import FakeEmbedder


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


async def test_the_vectors_have_the_width_of_the_database_column_by_default() -> None:
    embedder = FakeEmbedder()

    (vector,) = await embedder.embed(["A red square. Tags: red, square"])

    assert embedder.dimensions == 384
    assert len(vector) == 384


async def test_the_vectors_have_unit_length() -> None:
    texts = ["a dog in a park", "beach at sunset with waves", "x"]

    for vector in await FakeEmbedder().embed(texts):
        assert math.isclose(math.sqrt(sum(value * value for value in vector)), 1.0)


async def test_the_same_text_always_gives_the_same_vector() -> None:
    first = await FakeEmbedder().embed(["red house garden"])
    second = await FakeEmbedder().embed(["red house garden"])

    assert first == second


def test_the_vector_does_not_depend_on_the_process() -> None:
    """Python's own `hash()` changes between processes. The vector must not."""
    script = (
        "import asyncio, json\n"
        "from gallery.adapters.embeddings import FakeEmbedder\n"
        "print(json.dumps(asyncio.run(FakeEmbedder().embed(['red house garden']))))\n"
    )
    vectors = [
        json.loads(
            subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                check=True,
                env={"PYTHONHASHSEED": seed},
            ).stdout
        )
        for seed in ("1", "2")
    ]

    assert vectors[0] == vectors[1] == asyncio.run(FakeEmbedder().embed(["red house garden"]))


async def test_the_order_of_the_texts_decides_the_order_of_the_vectors() -> None:
    embedder = FakeEmbedder()

    together = await embedder.embed(["alpha beta", "gamma delta"])

    assert together == [
        (await embedder.embed(["alpha beta"]))[0],
        (await embedder.embed(["gamma delta"]))[0],
    ]


async def test_texts_that_share_words_score_higher_than_texts_that_do_not() -> None:
    garden, pool, ocean = await FakeEmbedder().embed(
        [
            "A red house with a garden. Tags: house, red, garden",
            "A red house with a pool. Tags: house, red, pool",
            "Blue ocean waves at dusk. Tags: ocean, waves, blue",
        ]
    )

    assert cosine(garden, pool) > cosine(garden, ocean)
    assert cosine(garden, pool) > 0.3
    assert abs(cosine(garden, ocean)) < 0.3


async def test_a_text_scores_one_against_itself_and_ignores_case_and_punctuation() -> None:
    plain, shouted = await FakeEmbedder().embed(["Red house, big garden!", "red HOUSE big garden"])

    assert math.isclose(cosine(plain, shouted), 1.0)


async def test_a_text_without_words_still_gets_a_valid_unit_vector() -> None:
    (empty,) = await FakeEmbedder().embed([""])

    assert math.isclose(sum(value * value for value in empty), 1.0)


async def test_embedding_nothing_gives_nothing() -> None:
    assert await FakeEmbedder().embed([]) == []


def test_the_model_id_says_it_is_fake() -> None:
    # Stored with every vector, so a fake vector is never compared with a real model's.
    assert FakeEmbedder().model_id != "BAAI/bge-small-en-v1.5"
    assert FakeEmbedder().model_id.startswith("fake:")
