"""TextEmbedder adapters and the factory that picks one from settings."""

from gallery.adapters.embeddings.fake import FakeEmbedder
from gallery.adapters.embeddings.fastembed import FastEmbedEmbedder
from gallery.config import Settings
from gallery.ports import TextEmbedder

__all__ = ["DATABASE_DIMENSIONS", "FakeEmbedder", "FastEmbedEmbedder", "build_embedder"]

# The width of `image_metadata.embedding` (20261020000008_image_embeddings.sql).
DATABASE_DIMENSIONS = 384


def build_embedder(settings: Settings) -> TextEmbedder | None:
    """Return the embedder that `settings.EMBEDDER` names, or None when it is "none".

    Raises `ValueError` when the chosen model isn't usable, so a misconfigured worker stops at
    startup instead of warning on every job. That covers a model fastembed doesn't know and one
    whose vectors don't fit the database column.
    """
    match settings.EMBEDDER:
        case "none":
            return None
        case "fastembed":
            embedder = FastEmbedEmbedder(
                model=settings.EMBEDDING_MODEL, cache_dir=settings.EMBEDDING_CACHE_DIR
            )
            if embedder.dimensions != DATABASE_DIMENSIONS:
                raise ValueError(
                    f"EMBEDDING_MODEL={settings.EMBEDDING_MODEL} makes {embedder.dimensions}-"
                    f"dimensional vectors, but the database column holds {DATABASE_DIMENSIONS}"
                )
            return embedder
