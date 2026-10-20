"""The backfill against in-memory fakes: paging, batching, idempotence and failure handling."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from gallery import backfill_embeddings
from gallery.adapters.embeddings import FakeEmbedder
from gallery.backfill_embeddings import BATCH_SIZE, BackfillCounts, BacklogRow, backfill
from gallery.config import Settings
from gallery.domain.embedding_text import embedding_text


def make_row(image_id: int, *, tags: tuple[str, ...] | None = None, text: str = "") -> BacklogRow:
    return BacklogRow(
        image_id=image_id,
        tags=("tag", f"image{image_id}") if tags is None else tags,
        description=text or f"Picture number {image_id}.",
    )


@dataclass
class InMemoryLibrary:
    """Plays the database: it lists the rows that have no embedding and stores new ones.

    Storing an embedding removes the row from the backlog, which is what makes a second run find
    nothing to do. `broken_ids` can't be stored, whatever is tried.
    """

    rows: list[BacklogRow]
    broken_ids: set[int] = field(default_factory=set)
    stored: dict[int, tuple[tuple[float, ...], str]] = field(default_factory=dict)
    page_calls: list[tuple[int, int]] = field(default_factory=list)

    async def page(self, after_image_id: int, limit: int) -> list[BacklogRow]:
        self.page_calls.append((after_image_id, limit))
        pending = [
            row
            for row in self.rows
            if row.image_id > after_image_id and row.image_id not in self.stored
        ]
        return sorted(pending, key=lambda row: row.image_id)[:limit]

    async def claim(self, limit: int) -> list[object]:
        raise NotImplementedError

    async def complete(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError

    async def fail(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError

    async def store_embedding(self, image_id: int, vector: Sequence[float], model_id: str) -> None:
        if image_id in self.broken_ids:
            raise RuntimeError(f"cannot store {image_id}")
        self.stored[image_id] = (tuple(vector), model_id)


class RecordingEmbedder(FakeEmbedder):
    """A FakeEmbedder that notes each call, and can be made to fail on chosen ones."""

    def __init__(self, *, fail_on_calls: frozenset[int] = frozenset()) -> None:
        super().__init__()
        self.calls: list[list[str]] = []
        self._fail_on_calls = fail_on_calls

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if len(self.calls) in self._fail_on_calls:
            raise RuntimeError("the model fell over")
        return await super().embed(texts)


@pytest.fixture(autouse=True)
def capture_backfill_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="gallery.backfill_embeddings")


async def test_it_embeds_every_row_and_stores_each_vector_with_the_model_id() -> None:
    library = InMemoryLibrary([make_row(1), make_row(2), make_row(3)])
    embedder = RecordingEmbedder()

    counts = await backfill(library, embedder, library)

    assert counts == BackfillCounts(scanned=3, embedded=3, skipped=0, failed=0)
    assert sorted(library.stored) == [1, 2, 3]
    assert {model for _, model in library.stored.values()} == {embedder.model_id}
    (expected_vector,) = await embedder.embed(
        [embedding_text(("tag", "image2"), "Picture number 2.")]
    )
    assert library.stored[2][0] == tuple(expected_vector)


async def test_it_pages_through_the_backlog_in_batches_of_32() -> None:
    library = InMemoryLibrary([make_row(image_id) for image_id in range(1, 71)])
    embedder = RecordingEmbedder()

    counts = await backfill(library, embedder, library)

    assert BATCH_SIZE == 32
    assert counts.embedded == 70
    assert [len(texts) for texts in embedder.calls] == [32, 32, 6]
    # Each page starts after the last row of the previous one, and the last empty page ends the run.
    assert library.page_calls == [(0, 32), (32, 32), (64, 32), (70, 32)]


async def test_it_does_nothing_when_nothing_needs_an_embedding() -> None:
    library = InMemoryLibrary([])
    embedder = RecordingEmbedder()

    counts = await backfill(library, embedder, library)

    assert counts == BackfillCounts()
    assert embedder.calls == []


async def test_a_second_run_finds_nothing_left_to_do() -> None:
    library = InMemoryLibrary([make_row(image_id) for image_id in range(1, 41)])
    embedder = RecordingEmbedder()

    first = await backfill(library, embedder, library)
    second = await backfill(library, embedder, library)

    assert first.embedded == 40
    assert second == BackfillCounts()
    assert len(embedder.calls) == 2  # only the first run's two batches


async def test_a_run_that_was_cut_short_picks_up_where_the_rows_are_missing() -> None:
    library = InMemoryLibrary([make_row(image_id) for image_id in range(1, 11)])
    library.stored[1] = ((1.0,), "earlier-run")
    library.stored[2] = ((1.0,), "earlier-run")
    embedder = RecordingEmbedder()

    counts = await backfill(library, embedder, library)

    assert counts.scanned == 8
    assert library.stored[1] == ((1.0,), "earlier-run")
    assert sorted(library.stored) == list(range(1, 11))


async def test_a_row_that_cannot_be_stored_is_counted_and_does_not_stall_the_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    library = InMemoryLibrary([make_row(image_id) for image_id in range(1, 6)], broken_ids={3})

    counts = await backfill(library, RecordingEmbedder(), library)

    assert counts == BackfillCounts(scanned=5, embedded=4, skipped=0, failed=1)
    assert sorted(library.stored) == [1, 2, 4, 5]
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.image_id == 3


async def test_a_failed_batch_is_counted_and_the_next_one_still_runs() -> None:
    library = InMemoryLibrary([make_row(image_id) for image_id in range(1, 71)])
    embedder = RecordingEmbedder(fail_on_calls=frozenset({2}))

    counts = await backfill(library, embedder, library)

    assert counts == BackfillCounts(scanned=70, embedded=38, skipped=0, failed=32)
    assert sorted(library.stored) == [*range(1, 33), *range(65, 71)]


async def test_rows_without_any_text_are_skipped() -> None:
    empty = BacklogRow(image_id=2, tags=(), description="")
    library = InMemoryLibrary([make_row(1), empty, make_row(3)])
    embedder = RecordingEmbedder()

    counts = await backfill(library, embedder, library)

    assert counts == BackfillCounts(scanned=3, embedded=2, skipped=1, failed=0)
    assert sorted(library.stored) == [1, 3]
    assert len(embedder.calls[0]) == 2


async def test_a_batch_of_only_empty_rows_never_calls_the_embedder() -> None:
    library = InMemoryLibrary([BacklogRow(image_id=1, tags=(), description="")])
    embedder = RecordingEmbedder()

    counts = await backfill(library, embedder, library)

    assert counts.skipped == 1
    assert embedder.calls == []


async def test_it_logs_progress_after_each_batch(caplog: pytest.LogCaptureFixture) -> None:
    library = InMemoryLibrary([make_row(image_id) for image_id in range(1, 41)])

    await backfill(library, RecordingEmbedder(), library)

    progress = [r for r in caplog.records if r.getMessage() == "backfill progress"]
    assert [(r.scanned, r.embedded) for r in progress] == [(32, 32), (40, 40)]


async def test_it_embeds_the_description_and_tags_text_of_each_row() -> None:
    library = InMemoryLibrary([make_row(1, tags=("dog", "puppy"), text="A puppy in a park.")])
    embedder = RecordingEmbedder()

    await backfill(library, embedder, library)

    assert embedder.calls == [["A puppy in a park. Tags: dog, puppy"]]


async def test_a_run_needs_an_embedder(settings: Settings) -> None:
    assert settings.EMBEDDER == "none"

    with pytest.raises(ValueError, match="EMBEDDER=fastembed"):
        await backfill_embeddings.run(settings)


def test_the_exit_code_is_one_when_a_row_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_run(settings: Settings) -> BackfillCounts:
        return BackfillCounts(scanned=2, embedded=1, failed=1)

    monkeypatch.setattr(backfill_embeddings, "run", failing_run)
    monkeypatch.setattr(backfill_embeddings, "init_observability", lambda *args: None)

    with pytest.raises(SystemExit) as exit_info:
        backfill_embeddings.main()

    assert exit_info.value.code == 1


def test_the_exit_code_is_zero_when_everything_was_embedded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def clean_run(settings: Settings) -> BackfillCounts:
        return BackfillCounts(scanned=3, embedded=3)

    monkeypatch.setattr(backfill_embeddings, "run", clean_run)
    monkeypatch.setattr(backfill_embeddings, "init_observability", lambda *args: None)

    with pytest.raises(SystemExit) as exit_info:
        backfill_embeddings.main()

    assert exit_info.value.code == 0
    (summary,) = [r for r in caplog.records if r.getMessage().startswith("backfill finished")]
    assert "3 scanned, 3 embedded, 0 skipped, 0 failed" in summary.getMessage()
