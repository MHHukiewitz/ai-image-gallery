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
        (
            "A golden retriever sleeps on the sofa. It looks calm.",
            "A golden retriever sleeps on the sofa.",
        ),
        ("A cat sleeps on a sofa", "A cat sleeps on a sofa"),
        # A full stop with no space after it is not the end of a sentence.
        (
            "The 3.5 metre wave rolls in over the reef. Big.",
            "The 3.5 metre wave rolls in over the reef.",
        ),
    ],
)
def test_description_keeps_only_the_first_sentence(raw: str, expected: str) -> None:
    assert normalize_description(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Dogs. A brown dog runs across the grass.", "Dogs. A brown dog runs across the grass."),
        (
            "Sunset. Orange light spills over the bay. Gulls wait.",
            "Sunset. Orange light spills over the bay.",
        ),
        # "Dogs and cats play." is 19 characters, which is still short.
        ("Dogs and cats play. A brown dog runs too.", "Dogs and cats play. A brown dog runs too."),
        # "Dogs and cats sleep." is 20 characters, which is long enough to stand alone.
        ("Dogs and cats sleep. A brown dog runs too.", "Dogs and cats sleep."),
        ("Dogs.", "Dogs."),
    ],
)
def test_short_first_sentence_keeps_the_next_sentence_too(raw: str, expected: str) -> None:
    assert normalize_description(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "A painting of St. Augustine holding a book. Second sentence.",
            "A painting of St. Augustine holding a book.",
        ),
        ("A snowy Mt. Fuji rises at dawn. Clouds drift below.", "A snowy Mt. Fuji rises at dawn."),
        ("Mt. Fuji at dawn.", "Mt. Fuji at dawn."),
        ("Ask Dr. Smith about the rash today. Then wait.", "Ask Dr. Smith about the rash today."),
        (
            "Mr. Jones and Ms. Lee walk their old dog. It is slow.",
            "Mr. Jones and Ms. Lee walk their old dog.",
        ),
        (
            "Mrs. Patel waters the garden every morning. It blooms.",
            "Mrs. Patel waters the garden every morning.",
        ),
        (
            "Martin Luther King Jr. Park is busy at noon. Kids play.",
            "Martin Luther King Jr. Park is busy at noon.",
        ),
        (
            "Visitors gather at the Sr. Library in the square. It is quiet.",
            "Visitors gather at the Sr. Library in the square.",
        ),
        (
            "Room No. Twelve faces the harbour at night. It has a balcony.",
            "Room No. Twelve faces the harbour at night.",
        ),
        (
            "Dogs vs. Cats is painted on the old wall. Its colours are bright.",
            "Dogs vs. Cats is painted on the old wall.",
        ),
        (
            "Pets such as dogs, e.g. Cats and birds, sleep here. They nap.",
            "Pets such as dogs, e.g. Cats and birds, sleep here.",
        ),
        (
            "Pets such as dogs, e.g. cats and birds, sleep here.",
            "Pets such as dogs, e.g. cats and birds, sleep here.",
        ),
        (
            "Pets, i.e. Cats and birds, sleep indoors. They nap.",
            "Pets, i.e. Cats and birds, sleep indoors.",
        ),
        (
            "Pots, pans, etc. Hang on the kitchen wall. They are old.",
            "Pots, pans, etc. Hang on the kitchen wall.",
        ),
        (
            "A church (St. Mark's) glows at dusk. Lights are on.",
            "A church (St. Mark's) glows at dusk.",
        ),
        (
            "A portrait of J. K. Rowling signing books. Smiling widely.",
            "A portrait of J. K. Rowling signing books.",
        ),
        (
            "Written by J.R.R. Tolkien in Oxford. It sold well.",
            "Written by J.R.R. Tolkien in Oxford.",
        ),
        # Only the capitalised "No." is an abbreviation, so a lowercase "no." still ends a sentence.
        (
            "A man shakes his head and says no. Then he leaves the room.",
            "A man shakes his head and says no.",
        ),
    ],
)
def test_description_does_not_end_at_an_abbreviation_or_initial(raw: str, expected: str) -> None:
    assert normalize_description(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "Is this really a cat sleeping on the sofa? Yes, it is.",
            "Is this really a cat sleeping on the sofa?",
        ),
        (
            "What a lovely view of the harbour at dusk! Truly.",
            "What a lovely view of the harbour at dusk!",
        ),
    ],
)
def test_description_ends_at_bang_or_question_mark_before_a_capital(
    raw: str, expected: str
) -> None:
    assert normalize_description(raw) == expected


def test_full_stop_before_a_lowercase_letter_does_not_end_the_sentence() -> None:
    raw = "The wave rolls in over the reef. then it breaks."
    assert normalize_description(raw) == raw


def test_description_is_capped_at_300_characters() -> None:
    result = normalize_description("x" * 500)
    assert result == "x" * MAX_DESCRIPTION_CHARS


def test_description_cap_never_leaves_trailing_space() -> None:
    result = normalize_description("a " * 200)
    assert len(result) <= MAX_DESCRIPTION_CHARS
    assert result == result.rstrip()


def test_empty_description_stays_empty() -> None:
    assert normalize_description("   \n ") == ""
