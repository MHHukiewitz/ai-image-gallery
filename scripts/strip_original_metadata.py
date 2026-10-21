"""Strip the metadata from the originals stored before uploads were stripped (#85).

Uploads are stripped as they arrive (`gallery.domain.metadata`), so run this once, after that API
is deployed. It needs the Supabase secret key and the gallery package, so it runs in the worker
image. The image doesn't contain `scripts/`, so mount it:

    docker compose -f deploy/compose.prod.yml run --rm -v "$PWD/scripts:/scripts:ro" \\
        worker python /scripts/strip_original_metadata.py

By default it is a dry run. It downloads every original and reports how many carry a GPS position,
how many carry other EXIF, and how many would change for another reason (XMP, IPTC, comments, or
frames after the image). `--apply` also overwrites each original that changes at the same path and
updates its `size_bytes`. `--user-id <uuid>` limits either to one user's originals, to try it on
your own images first.

- **Idempotent.** A stripped original comes back unchanged, and `size_bytes` is corrected whenever
  it is off. A second run, or one that was interrupted, only does what is left.
- **Private.** It logs counts and image ids, never a tag value.
- **Safe around deletes.** Storage's replace recreates an object that was deleted, so after each
  replace it checks that the image's row still exists, and removes the object again if it doesn't.
  The API deletes the row before the objects, so either this check or the user's own delete
  removes the file.
- `content_sha256` stays the hash of the uploaded file, which the web client compares to find
  duplicates.

The exit code is 1 when any original failed. Locally, run it from `api/` with the stack's secret
key in `.env`: `uv run python ../scripts/strip_original_metadata.py`.
"""

import argparse
import asyncio
import logging
import sys
from dataclasses import asdict, dataclass
from typing import Protocol
from uuid import UUID

from gallery.adapters.supabase_client import AsyncClient, close_client, service_client
from gallery.adapters.supabase_storage import SupabaseObjectStore
from gallery.config import Settings, get_settings
from gallery.domain.metadata import strip_metadata, summarize_exif
from gallery.observability import init_observability

logger = logging.getLogger(__name__)

BATCH_SIZE = 50


@dataclass(frozen=True, slots=True)
class StoredOriginal:
    image_id: int
    path: str
    mime_type: str
    size_bytes: int


@dataclass(slots=True)
class StripCounts:
    scanned: int = 0
    # Each original lands in exactly one of these four.
    gps: int = 0
    other_exif: int = 0
    other_metadata: int = 0
    clean: int = 0
    # With --apply: objects overwritten, rows whose size_bytes was corrected, and images deleted
    # by their owner while the run was writing them.
    rewritten: int = 0
    resized: int = 0
    deleted: int = 0
    failed: int = 0


class Originals(Protocol):
    async def page(self, after_image_id: int, limit: int) -> list[StoredOriginal]:
        """Return up to `limit` originals in image id order, after the given id."""
        ...

    async def download(self, original: StoredOriginal) -> bytes: ...

    async def replace(self, original: StoredOriginal, data: bytes) -> bool:
        """Overwrite the object. False, with no object left behind, when the image is gone."""
        ...

    async def record_size(self, original: StoredOriginal, size_bytes: int) -> None: ...


class SupabaseOriginals:
    """Every user's originals, or one user's. The client must hold the secret key (no RLS)."""

    def __init__(self, client: AsyncClient, bucket: str, *, user_id: UUID | None = None) -> None:
        self._client = client
        self._store = SupabaseObjectStore(client, bucket)
        self._bucket = client.storage.from_(bucket)
        self._user_id = user_id

    async def page(self, after_image_id: int, limit: int) -> list[StoredOriginal]:
        query = (
            self._client.table("images")
            .select("id, original_path, mime_type, size_bytes")
            .gt("id", after_image_id)
        )
        if self._user_id is not None:
            query = query.eq("user_id", str(self._user_id))
        response = await query.order("id").limit(limit).execute()
        return [
            StoredOriginal(
                image_id=row["id"],
                path=row["original_path"],
                mime_type=row["mime_type"],
                size_bytes=row["size_bytes"],
            )
            for row in response.data
        ]

    async def download(self, original: StoredOriginal) -> bytes:
        # The adapter retries transient Storage errors, which a long run is bound to meet.
        return await self._store.get(original.path)

    async def replace(self, original: StoredOriginal, data: bytes) -> bool:
        await self._bucket.update(original.path, data, {"content-type": original.mime_type})
        # The PUT also creates the object when the owner deleted it in the meantime. The API
        # deletes the row first, so a missing row means the write left an orphan: remove it.
        response = await (
            self._client.table("images").select("id").eq("id", original.image_id).execute()
        )
        if response.data:
            return True
        await self._store.delete([original.path])
        return False

    async def record_size(self, original: StoredOriginal, size_bytes: int) -> None:
        await (
            self._client.table("images")
            .update({"size_bytes": size_bytes})
            .eq("id", original.image_id)
            .execute()
        )


async def strip_originals(
    originals: Originals, *, apply: bool, batch_size: int = BATCH_SIZE
) -> StripCounts:
    """Look at every original, and with `apply`, strip those that carry metadata.

    Paging moves forward by image id, so an original that keeps failing is passed over once and
    can't stall the run. A failure is counted and logged, and the run goes on.
    """
    counts = StripCounts()
    after_image_id = 0
    while page := await originals.page(after_image_id, batch_size):
        after_image_id = page[-1].image_id
        for original in page:
            counts.scanned += 1
            await _strip_one(originals, original, apply=apply, counts=counts)
        logger.info("progress", extra=asdict(counts))
    return counts


async def _strip_one(
    originals: Originals, original: StoredOriginal, *, apply: bool, counts: StripCounts
) -> None:
    try:
        data = await originals.download(original)
        summary = summarize_exif(data, original.mime_type)
        stripped = strip_metadata(data, original.mime_type)
    except Exception:
        logger.warning(
            "could not read an original", exc_info=True, extra={"image_id": original.image_id}
        )
        counts.failed += 1
        return
    if summary.gps:
        counts.gps += 1
    elif summary.other_tags:
        counts.other_exif += 1
    elif stripped != data:
        counts.other_metadata += 1
    else:
        counts.clean += 1
    if not apply:
        return
    try:
        if stripped != data:
            if not await originals.replace(original, stripped):
                counts.deleted += 1
                return
            counts.rewritten += 1
        # Also when the object was stripped by an earlier run that stopped before this step.
        if original.size_bytes != len(stripped):
            await originals.record_size(original, len(stripped))
            counts.resized += 1
    except Exception:
        logger.warning(
            "could not rewrite an original", exc_info=True, extra={"image_id": original.image_id}
        )
        counts.failed += 1


async def run(settings: Settings, *, apply: bool, user_id: UUID | None = None) -> StripCounts:
    """Build the Supabase client from `settings` and go through the originals."""
    client = await service_client(settings)
    try:
        originals = SupabaseOriginals(client, settings.STORAGE_BUCKET, user_id=user_id)
        return await strip_originals(originals, apply=apply)
    finally:
        await close_client(client)


def main(argv: list[str] | None = None) -> None:
    """Parse the arguments, run, log the counts, and exit 1 when any original failed."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="overwrite the originals that carry metadata (default: only count them)",
    )
    parser.add_argument(
        "--user-id", type=UUID, help="only this user's originals (default: every user's)"
    )
    arguments = parser.parse_args(argv)
    settings = get_settings()
    # It runs in the worker image and reports as the worker.
    init_observability(settings, "worker")
    counts = asyncio.run(run(settings, apply=arguments.apply, user_id=arguments.user_id))
    logger.info(
        "%s: %d originals, %d with GPS, %d with other EXIF, %d with other metadata, %d clean; "
        "%d rewritten, %d sizes corrected, %d deleted meanwhile, %d failed",
        "applied" if arguments.apply else "dry run",
        counts.scanned,
        counts.gps,
        counts.other_exif,
        counts.other_metadata,
        counts.clean,
        counts.rewritten,
        counts.resized,
        counts.deleted,
        counts.failed,
        extra=asdict(counts),
    )
    sys.exit(1 if counts.failed else 0)


if __name__ == "__main__":
    main()
