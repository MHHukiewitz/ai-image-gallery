import pytest

from gallery.domain.tags import (
    MAX_DESCRIPTION_CHARS,
    MAX_TAGS,
    normalize_description,
    normalize_tags,
)


def test_lowercases_trims_and_collapses_whitespace() -> None:
    assert normalize_tags(["  Ocean ", "GOLDEN   Hour", "Sea\tBreeze"]) == (
        "ocean",
        "golden hour",
        "sea breeze",
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sunset!", "sunset"),
        ("#beach", "beach"),
        ("(boat)", "boat"),
        ("ocean, sunset", "ocean sunset"),
        ("sea/sky", "sea sky"),
        ("child's toy", "childs toy"),
        ("child’s toy", "childs toy"),
        ("snake_case", "snake case"),
        # Hyphens survive only inside a word.
        ("black-and-white", "black-and-white"),
        ("-leading and trailing-", "leading and trailing"),
        ("a - b", "a b"),
        ("well--known", "well-known"),
        ("Café", "café"),
    ],
)
def test_strips_punctuation_except_inner_hyphens(raw: str, expected: str) -> None:
    assert normalize_tags([raw]) == (expected,)


@pytest.mark.parametrize("raw", ["", "   ", "!!!", "-", "--", "?!  ..."])
def test_drops_tags_that_end_up_empty(raw: str) -> None:
    assert normalize_tags([raw, "kept"]) == ("kept",)


def test_drops_tags_longer_than_three_words() -> None:
    tags = normalize_tags(["two words", "three little words", "this is far too long", "ok"])
    assert tags == ("two words", "three little words", "ok")


def test_long_phrase_is_measured_after_cleanup() -> None:
    # Punctuation alone must not push a three-word tag over the limit.
    assert normalize_tags(["one, two, three!"]) == ("one two three",)


def test_deduplicates_after_normalization_and_keeps_first_position() -> None:
    assert normalize_tags(["Cat", "dog", "cat", "CAT!", " dog "]) == ("cat", "dog")


def test_clamps_to_ten_tags() -> None:
    raw = [f"tag{i}" for i in range(15)]
    assert normalize_tags(raw) == tuple(raw[:MAX_TAGS])


def test_duplicates_and_junk_do_not_use_up_clamp_slots() -> None:
    raw = ["a", "A", "!!!", *[f"tag{i}" for i in range(12)]]
    assert normalize_tags(raw) == ("a", *[f"tag{i}" for i in range(9)])


def test_accepts_any_iterable() -> None:
    assert normalize_tags(tag for tag in ["Red", "red", "Blue"]) == ("red", "blue")


def test_empty_input_gives_empty_tuple() -> None:
    assert normalize_tags([]) == ()


def test_description_is_trimmed_and_whitespace_collapsed() -> None:
    assert normalize_description("  A dog\n  runs   on grass.  ") == "A dog runs on grass."


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("A cat sleeps. It looks calm.", "A cat sleeps."),
        ("Is it a cat? Yes, it is.", "Is it a cat?"),
        ("What a view! Truly.", "What a view!"),
        ("A cat sleeps on a sofa", "A cat sleeps on a sofa"),
        # A full stop with no space after it is not the end of a sentence.
        ("A 3.5 metre wave. Big.", "A 3.5 metre wave."),
    ],
)
def test_description_keeps_only_the_first_sentence(raw: str, expected: str) -> None:
    assert normalize_description(raw) == expected


def test_description_is_capped_at_300_characters() -> None:
    result = normalize_description("x" * 500)
    assert result == "x" * MAX_DESCRIPTION_CHARS


def test_description_cap_never_leaves_trailing_space() -> None:
    result = normalize_description("a " * 200)
    assert len(result) <= MAX_DESCRIPTION_CHARS
    assert result == result.rstrip()


def test_empty_description_stays_empty() -> None:
    assert normalize_description("   \n ") == ""
