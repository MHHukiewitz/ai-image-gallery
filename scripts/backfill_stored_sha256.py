"""Fill in images.stored_sha256 for images stored before the API wrote it (#113).

The API writes stored_sha256, the hash of the stored, metadata-stripped original, with every new
image. Rows from before that, and rows the old API wrote between the migration and the deploy,
have none, so they are found as duplicates only by content_sha256. Run this once, after the API
that writes the column is deployed. It needs the Supabase secret key and the gallery package, so
it runs in the worker image. The image doesn't contain `scripts/`, so mount it:

    docker compose -f deploy/compose.prod.yml run --rm -v "$PWD/scripts:/scripts:ro" \\
        worker python /scripts/backfill_stored_sha256.py

By default it is a dry run that counts the images without a stored hash. `--apply` downloads each
of their originals, hashes it, and sets the column.

- **The stored bytes are the input.** Every original is stored stripped: new uploads since #85,
  older ones since `strip_original_metadata.py` ran. So the hash of what is stored is the stripped
  file's hash, which is what the API computes for a new upload.
- **Idempotent.** It reads only rows whose stored_sha256 is still empty, so a second run, or one
  that was interrupted, only does what is left.
- **Private.** It logs counts and image ids, never a hash.

The exit code is 1 when any image failed. Locally, run it from `api/` with the stack's secret key
in `.env`: `uv run python ../scripts/backfill_stored_sha256.py`.
"""

import argparse
import asyncio
import hashlib
import logging
import sys
from dataclasses import asdict, dataclass
from typing import Protocol

from gallery.adapters.supabase_client import AsyncClient, close_client, service_client
from gallery.adapters.supabase_storage import SupabaseObjectStore
from gallery.config import Settings, get_settings
from gallery.observability import init_observability

logger = logging.getLogger(__name__)

BATCH_SIZE = 50


@dataclass(frozen=True, slots=True)
class UnhashedImage:
    image_id: int
    original_path: str


@dataclass(slots=True)
class BackfillCounts:
    # Images without a stored hash when the run looked.
    missing: int = 0
    # With --apply: hashes written, and images that couldn't be read or written.
    written: int = 0
    failed: int = 0


class Images(Protocol):
    async def page(self, after_image_id: int, limit: int) -> list[UnhashedImage]:
        """Return up to `limit` images without a stored hash, in id order, after the given id."""
        ...

    async def download(self, image: UnhashedImage) -> bytes: ...

    async def record(self, image: UnhashedImage, stored_sha256: str) -> None:
        """Set the stored hash, unless another run or a re-upload has set it meanwhile."""
        ...


class SupabaseImages:
    """Every user's images. The client must hold the secret key (no RLS)."""

    def __init__(self, client: AsyncClient, bucket: str) -> None:
        self._client = client
        self._store = SupabaseObjectStore(client, bucket)

    async def page(self, after_image_id: int, limit: int) -> list[UnhashedImage]:
        response = await (
            self._client.table("images")
            .select("id, original_path")
            .is_("stored_sha256", "null")
            .gt("id", after_image_id)
            .order("id")
            .limit(limit)
            .execute()
        )
        return [UnhashedImage(row["id"], row["original_path"]) for row in response.data]

    async def download(self, image: UnhashedImage) -> bytes:
        # The adapter retries transient Storage errors, which a long run is bound to meet.
        return await self._store.get(image.original_path)

    async def record(self, image: UnhashedImage, stored_sha256: str) -> None:
        await (
            self._client.table("images")
            .update({"stored_sha256": stored_sha256})
            .eq("id", image.image_id)
            .is_("stored_sha256", "null")
            .execute()
        )


async def backfill(images: Images, *, apply: bool, batch_size: int = BATCH_SIZE) -> BackfillCounts:
    """Count the images without a stored hash, and with `apply`, hash their originals.

    Paging moves forward by image id, so an image that keeps failing is passed over once and can't
    stall the run. A failure is counted and logged, and the run goes on.
    """
    counts = BackfillCounts()
    after_image_id = 0
    while page := await images.page(after_image_id, batch_size):
        after_image_id = page[-1].image_id
        for image in page:
            counts.missing += 1
            if apply:
                await _hash_one(images, image, counts)
        logger.info("progress", extra=asdict(counts))
    return counts


async def _hash_one(images: Images, image: UnhashedImage, counts: BackfillCounts) -> None:
    try:
        data = await images.download(image)
        await images.record(image, hashlib.sha256(data).hexdigest())
    except Exception:
        logger.warning(
            "could not hash an original", exc_info=True, extra={"image_id": image.image_id}
        )
        counts.failed += 1
        return
    counts.written += 1


async def run(settings: Settings, *, apply: bool) -> BackfillCounts:
    """Build the Supabase client from `settings` and go through the images."""
    client = await service_client(settings)
    try:
        return await backfill(SupabaseImages(client, settings.STORAGE_BUCKET), apply=apply)
    finally:
        await close_client(client)


def main(argv: list[str] | None = None) -> None:
    """Parse the arguments, run, log the counts, and exit 1 when any image failed."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="hash the originals and set stored_sha256 (default: only count them)",
    )
    arguments = parser.parse_args(argv)
    settings = get_settings()
    # It runs in the worker image and reports as the worker.
    init_observability(settings, "worker")
    counts = asyncio.run(run(settings, apply=arguments.apply))
    logger.info(
        "%s: %d images without a stored hash, %d written, %d failed",
        "applied" if arguments.apply else "dry run",
        counts.missing,
        counts.written,
        counts.failed,
        extra=asdict(counts),
    )
    if counts.failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
