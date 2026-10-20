"""The text that gets embedded for one image. Pure, no I/O."""

from collections.abc import Sequence


def embedding_text(tags: Sequence[str], description: str) -> str:
    """Return `"{description} Tags: {tag}, {tag}, ..."`, the text the embedding model reads.

    Pass the final tags, after the verifier, so the vector describes what the gallery shows. A
    missing description or tag list leaves its part out, and the result is empty when both are.
    """
    parts = []
    if description.strip():
        parts.append(description.strip())
    if tags:
        parts.append(f"Tags: {', '.join(tags)}")
    return " ".join(parts)
