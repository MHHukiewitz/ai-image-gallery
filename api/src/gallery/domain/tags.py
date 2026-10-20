"""Cleanup for model-written tags and descriptions. Pure functions, no I/O."""

import re
from collections.abc import Iterable, Sequence

from gallery.domain.models import TagVerdict

MIN_TAGS = 5
MAX_TAGS = 10
# The prompt asks for 1-2 words per tag. One extra word is tolerated rather than discarded.
MAX_TAG_WORDS = 3
MAX_DESCRIPTION_CHARS = 300
# One verification asks about at most this many overlapping pairs, to bound the request.
MAX_MERGE_QUESTIONS = 3

_APOSTROPHES = "'’"
# Every candidate sentence end. `_split_sentences` decides which of them really end a sentence.
_SENTENCE_END = re.compile(r"[.!?] ")
# Dotted initials such as "J." or "J.R.R.". A name written this way doesn't end a sentence.
_INITIALS = re.compile(r"(?:[A-Z]\.)+")
# Matched exactly as written. A lowercase "no." can end a sentence ("He said no. Then he left."),
# so the list stays capitalised and tight rather than matching case-insensitively.
_ABBREVIATIONS = frozenset(
    {"St.", "Mt.", "Dr.", "Mr.", "Mrs.", "Ms.", "Jr.", "Sr.", "vs.", "e.g.", "i.e.", "etc.", "No."}
)
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
    """Trim the text, keep only its first sentence and cap it at `MAX_DESCRIPTION_CHARS`.

    A sentence ends at `.`, `!` or `?` followed by a space and an uppercase letter, or at the end
    of the text. The same marks don't end one after an abbreviation or an initial, so
    "A painting of St. Augustine." stays whole.
    """
    text = " ".join(raw.split())
    first_sentence = _split_sentences(text)[0]
    return first_sentence[:MAX_DESCRIPTION_CHARS].rstrip()


def _split_sentences(text: str) -> list[str]:
    """Cut `text` into sentences. Always returns at least one part, which may be empty."""
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        # `text` has no trailing whitespace, so a character always follows the space.
        if not text[match.end()].isupper() or _ends_with_abbreviation(text, match.start()):
            continue
        sentences.append(text[start : match.start() + 1])
        start = match.end()
    sentences.append(text[start:])
    return sentences


def _ends_with_abbreviation(text: str, mark: int) -> bool:
    """Whether the word that ends with the punctuation at `text[mark]` is an abbreviation."""
    word_start = text.rfind(" ", 0, mark) + 1
    # Strip opening brackets and quotes so that "(St." still counts as "St.".
    word = text[word_start : mark + 1].lstrip("\"'([“‘")
    return word in _ABBREVIATIONS or _INITIALS.fullmatch(word) is not None


def duplicate_candidates(
    tags: Sequence[str], *, limit: int = MAX_MERGE_QUESTIONS
) -> tuple[tuple[str, str], ...]:
    """Pair up tags that probably say the same thing, such as "house" and "red house".

    Two tags overlap when every word of one is also a word of the other. Each tag joins at most
    one pair, so no merge depends on another, and at most `limit` pairs come back. A pair lists
    the tag that comes first in `tags` first.
    """
    unique = tuple(dict.fromkeys(tags))
    words = [frozenset(tag.split()) for tag in unique]
    paired: set[int] = set()
    pairs: list[tuple[str, str]] = []
    for i in range(len(unique)):
        if len(pairs) >= limit:
            break
        if i in paired:
            continue
        for j in range(i + 1, len(unique)):
            if j not in paired and (words[i] <= words[j] or words[j] <= words[i]):
                pairs.append((unique[i], unique[j]))
                paired.update((i, j))
                break
    return tuple(pairs)


def select_tags(
    tags: Sequence[str],
    verdict: TagVerdict,
    *,
    min_probability: float,
    min_count: int = MIN_TAGS,
    max_count: int = MAX_TAGS,
) -> tuple[str, ...]:
    """Apply a verifier's verdict to `tags`, keeping between `min_count` and `max_count` of them.

    1. Each merge `(kept, dropped)` removes `dropped`, unless `kept` scored below
       `min_probability`: that would lose a good tag in favour of a rejected one.
    2. Tags scored below `min_probability` are removed. A tag without a score stays.
    3. If fewer than `min_count` tags remain, the removed tags with the highest scores come back.
       A merged-away duplicate is a true tag, so it returns before a rejected one.
    4. At most `max_count` tags are kept, the first ones. The original order never changes.

    An empty verdict leaves the tags as they were. When `tags` itself has fewer than `min_count`
    entries, all of them are returned.
    """
    ordered = tuple(dict.fromkeys(tags))

    def probability(tag: str) -> float:
        # Without a score there is no evidence against the tag, so it sits at the threshold.
        return verdict.probabilities.get(tag, min_probability)

    merged_away: set[str] = set()
    for kept, dropped in verdict.merges:
        if (
            kept in ordered
            and dropped in ordered
            and kept != dropped
            and kept not in merged_away
            and dropped not in merged_away
            and probability(kept) >= min_probability
        ):
            merged_away.add(dropped)

    selected = {
        tag for tag in ordered if tag not in merged_away and probability(tag) >= min_probability
    }
    if len(selected) < min_count:
        removed = [tag for tag in ordered if tag not in selected]
        # `sorted` is stable, so equal scores keep the analyzer's order.
        by_score = sorted(removed, key=probability, reverse=True)
        selected.update(by_score[: min_count - len(selected)])
    return tuple(tag for tag in ordered if tag in selected)[:max_count]


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
