"""Embed every analyzed image that has no embedding yet.

Run it once after turning on ``EMBEDDER=fastembed``, in the worker image, because it needs the
Supabase secret key and the model:

    docker compose -f deploy/compose.prod.yml run --rm worker python -m gallery.backfill_embeddings

It pages through ``done`` rows with no embedding, embeds them 32 at a time and stores each vector,
then logs how many it embedded. It is idempotent: a row with an embedding is no longer picked up,
so a second run, or one that was interrupted, only does what is left. The exit code is 1 when any
row failed.
"""

import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import Protocol

from gallery.adapters.embeddings import build_embedder
from gallery.adapters.supabase_client import AsyncClient, close_client, service_client
from gallery.adapters.supabase_jobs import SupabaseJobQueue
from gallery.config import Settings, get_settings
from gallery.domain.embedding_text import embedding_text
from gallery.observability import init_observability
from gallery.ports import JobQueue, TextEmbedder

logger = logging.getLogger(__name__)

BATCH_SIZE = 32


@dataclass(frozen=True, slots=True)
class BacklogRow:
    """An analyzed image that still needs its embedding."""

    image_id: int
    tags: tuple[str, ...]
    description: str


@dataclass(slots=True)
class BackfillCounts:
    scanned: int = 0
    embedded: int = 0
    # Rows with neither tags nor a description, which have nothing to embed.
    skipped: int = 0
    failed: int = 0


class Backlog(Protocol):
    async def page(self, after_image_id: int, limit: int) -> list[BacklogRow]:
        """Return up to `limit` rows without an embedding, in image id order, after the given id."""
        ...


class SupabaseBacklog:
    """The backlog on `image_metadata`. The client must hold the secret key."""

    def __init__(self, client: AsyncClient) -> None:
        self._client = client

    async def page(self, after_image_id: int, limit: int) -> list[BacklogRow]:
        response = await (
            self._client.table("image_metadata")
            .select("image_id, tags, description")
            .eq("ai_processing_status", "done")
            .is_("embedding", "null")
            .gt("image_id", after_image_id)
            .order("image_id")
            .limit(limit)
            .execute()
        )
        return [
            BacklogRow(
                image_id=row["image_id"],
                tags=tuple(row["tags"]),
                description=row["description"] or "",
            )
            for row in response.data
        ]


async def backfill(
    backlog: Backlog,
    embedder: TextEmbedder,
    queue: JobQueue,
    *,
    batch_size: int = BATCH_SIZE,
) -> BackfillCounts:
    """Embed the backlog one batch at a time and return what happened.

    Paging moves forward by image id instead of re-reading the front of the backlog, so a row that
    keeps failing is passed over once and can't stall the run. A failed batch or row is counted and
    logged, and the run goes on.
    """
    counts = BackfillCounts()
    after_image_id = 0
    while rows := await backlog.page(after_image_id, batch_size):
        after_image_id = rows[-1].image_id
        counts.scanned += len(rows)
        await _embed_batch(rows, embedder, queue, counts)
        logger.info(
            "backfill progress",
            extra={
                "scanned": counts.scanned,
                "embedded": counts.embedded,
                "skipped": counts.skipped,
                "failed": counts.failed,
            },
        )
    return counts


async def _embed_batch(
    rows: list[BacklogRow], embedder: TextEmbedder, queue: JobQueue, counts: BackfillCounts
) -> None:
    texts = {row.image_id: embedding_text(row.tags, row.description) for row in rows}
    embeddable = [row for row in rows if texts[row.image_id]]
    counts.skipped += len(rows) - len(embeddable)
    if not embeddable:
        return
    try:
        vectors = await embedder.embed([texts[row.image_id] for row in embeddable])
    except Exception:
        logger.warning(
            "could not embed a batch, leaving it for the next run",
            exc_info=True,
            extra={"image_ids": [row.image_id for row in embeddable]},
        )
        counts.failed += len(embeddable)
        return
    for row, vector in zip(embeddable, vectors, strict=True):
        try:
            await queue.store_embedding(row.image_id, vector, embedder.model_id)
        except Exception:
            logger.warning(
                "could not store an embedding, leaving it for the next run",
                exc_info=True,
                extra={"image_id": row.image_id},
            )
            counts.failed += 1
        else:
            counts.embedded += 1


async def run(settings: Settings) -> BackfillCounts:
    """Build the adapters from `settings` and backfill the whole backlog."""
    embedder = build_embedder(settings)
    if embedder is None:
        raise ValueError("the backfill needs EMBEDDER=fastembed")
    client = await service_client(settings)
    try:
        queue = SupabaseJobQueue(
            client,
            stale_after_seconds=settings.JOB_STALE_AFTER_SECONDS,
            max_attempts=settings.JOB_MAX_ATTEMPTS,
        )
        return await backfill(SupabaseBacklog(client), embedder, queue)
    finally:
        await close_client(client)


def main() -> None:
    """Run the backfill and exit 1 when any row could not be embedded."""
    settings = get_settings()
    # The backfill runs in the worker image and reports as the worker.
    init_observability(settings, "worker")
    counts = asyncio.run(run(settings))
    logger.info(
        "backfill finished: %d scanned, %d embedded, %d skipped, %d failed",
        counts.scanned,
        counts.embedded,
        counts.skipped,
        counts.failed,
        extra={
            "scanned": counts.scanned,
            "embedded": counts.embedded,
            "skipped": counts.skipped,
            "failed": counts.failed,
        },
    )
    sys.exit(1 if counts.failed else 0)


if __name__ == "__main__":
    main()
