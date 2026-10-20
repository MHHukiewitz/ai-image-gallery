"""Display names for uploads. The name is shown in the UI only: storage keys never contain it."""

import re
import unicodedata

FALLBACK_FILENAME = "image"
# The `images.filename` column is varchar(255), which counts characters.
MAX_FILENAME_LENGTH = 255
# Browsers may send a full client path, with either kind of separator.
_PATH_SEPARATORS = re.compile(r"[\\/]")
# Keeps a cut-down name recognizable by its type. Longer "extensions" are just part of the name.
_MAX_EXTENSION_LENGTH = 10
# Bidirectional overrides and isolates (U+202A to U+202E, U+2066 to U+2069) can make a name read
# backwards, as in "photo-gpj.exe". Other format characters, like the joiner in emoji, are kept.
_BIDI_CONTROLS = frozenset("‪‫‬‭‮⁦⁧⁨⁩")


def display_filename(raw: str | None) -> str:
    """Return a safe name to show for an upload, whatever the client sent.

    Only the basename is kept. Control characters are dropped, since Postgres refuses NUL in text
    and the rest would garble the UI. The result has at most 255 characters and is never empty.
    """
    if not raw:
        return FALLBACK_FILENAME
    basename = _PATH_SEPARATORS.split(raw)[-1]
    cleaned = "".join(
        character
        for character in basename
        # Cc: control characters. Cs: lone surrogates, which can't be encoded as UTF-8.
        if unicodedata.category(character) not in {"Cc", "Cs"} and character not in _BIDI_CONTROLS
    ).strip()
    # A name of only dots, like "..", would read as a path, not a file.
    if not cleaned.strip("."):
        return FALLBACK_FILENAME
    return _truncate(cleaned)


def _truncate(name: str) -> str:
    """Cut `name` to the column limit, keeping a short extension so the file type stays visible."""
    if len(name) <= MAX_FILENAME_LENGTH:
        return name
    stem, dot, extension = name.rpartition(".")
    if dot and stem and len(extension) <= _MAX_EXTENSION_LENGTH:
        suffix = dot + extension
        return stem[: MAX_FILENAME_LENGTH - len(suffix)] + suffix
    return name[:MAX_FILENAME_LENGTH]
