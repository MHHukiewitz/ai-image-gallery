"""Cleanup for model-written tags and descriptions. Pure functions, no I/O."""

import re
from collections.abc import Iterable

MIN_TAGS = 5
MAX_TAGS = 10
# The prompt asks for 1-2 words per tag. One extra word is tolerated rather than discarded.
MAX_TAG_WORDS = 3
MAX_DESCRIPTION_CHARS = 300

_APOSTROPHES = "'’"
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_REPEATED_HYPHENS = re.compile(r"-{2,}")


def normalize_tags(raw: Iterable[str]) -> tuple[str, ...]:
    """Lowercase, clean, deduplicate and clamp tags, keeping the model's order.

    Punctuation is dropped except hyphens inside a word. Tags that end up empty or longer than
    `MAX_TAG_WORDS` words are discarded, and at most `MAX_TAGS` distinct tags are returned.
    """
    tags: dict[str, None] = {}
    for candidate in raw:
        tag = _clean_tag(candidate)
        if tag and len(tag.split()) <= MAX_TAG_WORDS:
            tags.setdefault(tag)
        if len(tags) == MAX_TAGS:
            break
    return tuple(tags)


def normalize_description(raw: str) -> str:
    """Trim the text, keep only its first sentence and cap it at `MAX_DESCRIPTION_CHARS`."""
    text = " ".join(raw.split())
    first_sentence = _SENTENCE_BREAK.split(text, maxsplit=1)[0]
    return first_sentence[:MAX_DESCRIPTION_CHARS].rstrip()


def _clean_tag(raw: str) -> str:
    text = raw.lower()
    for apostrophe in _APOSTROPHES:
        # "child's" must become "childs", not "child s".
        text = text.replace(apostrophe, "")
    # Other punctuation separates words ("sea/sky"), so it becomes a space.
    text = "".join(ch if ch.isalnum() or ch.isspace() or ch == "-" else " " for ch in text)
    text = _REPEATED_HYPHENS.sub("-", text)
    words = (word.strip("-") for word in text.split())
    return " ".join(word for word in words if word)
