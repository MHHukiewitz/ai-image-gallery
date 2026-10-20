from gallery.domain.embedding_text import embedding_text


def test_joins_the_description_and_the_tags() -> None:
    text = embedding_text(("red", "square", "red house"), "A red square on a white wall.")

    assert text == "A red square on a white wall. Tags: red, square, red house"


def test_keeps_the_tags_in_the_given_order() -> None:
    assert embedding_text(("b", "a", "c"), "Desc.") == "Desc. Tags: b, a, c"


def test_leaves_out_a_missing_description() -> None:
    assert embedding_text(("dog", "puppy"), "") == "Tags: dog, puppy"
    assert embedding_text(("dog", "puppy"), "   ") == "Tags: dog, puppy"


def test_leaves_out_missing_tags() -> None:
    assert embedding_text((), "A dog in a park.") == "A dog in a park."


def test_is_empty_without_a_description_and_tags() -> None:
    assert embedding_text((), "") == ""


def test_trims_the_description() -> None:
    assert embedding_text(("dog",), "  A dog.\n") == "A dog. Tags: dog"
