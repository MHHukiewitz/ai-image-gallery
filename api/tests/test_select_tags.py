"""Tests for the pure rules that apply a tag verifier's verdict (domain/tags.py)."""

import pytest

from gallery.domain.models import TagVerdict
from gallery.domain.tags import (
    MAX_MERGE_QUESTIONS,
    MAX_TAGS,
    MIN_TAGS,
    duplicate_candidates,
    select_tags,
)

MIN_PROBABILITY = 0.5

SEVEN = ("house", "sun", "hill", "tree", "road", "cat", "beach")


def verdict(probabilities: dict[str, float] | None = None, *merges: tuple[str, str]) -> TagVerdict:
    return TagVerdict(probabilities=probabilities or {}, merges=merges)


def select(tags: tuple[str, ...], tag_verdict: TagVerdict, **options: int) -> tuple[str, ...]:
    return select_tags(tags, tag_verdict, min_probability=MIN_PROBABILITY, **options)


# --- select_tags: the verdict is applied -------------------------------------------------------


def test_an_empty_verdict_leaves_the_tags_unchanged() -> None:
    assert select(SEVEN, verdict()) == SEVEN


def test_a_merge_removes_the_dropped_tag_and_keeps_the_other() -> None:
    tags = ("house", "red house", "sun", "hill", "tree", "road")

    assert select(tags, verdict({}, ("red house", "house"))) == (
        "red house",
        "sun",
        "hill",
        "tree",
        "road",
    )


def test_a_merge_does_not_depend_on_which_tag_comes_first() -> None:
    tags = ("house", "red house", "sun", "hill", "tree", "road")

    assert "red house" not in select(tags, verdict({}, ("house", "red house")))
    assert "house" not in select(tags, verdict({}, ("red house", "house")))


def test_a_merge_naming_an_unknown_tag_is_ignored() -> None:
    assert select(SEVEN, verdict({}, ("house", "castle"), ("castle", "sun"))) == SEVEN


def test_a_merge_into_a_rejected_tag_is_ignored() -> None:
    """Merging "house" away for a "red house" that isn't in the photo would lose both."""
    tags = ("house", "red house", "sun", "hill", "tree", "road")
    probabilities = {"house": 0.97, "red house": 0.2}

    assert select(tags, verdict(probabilities, ("red house", "house"))) == (
        "house",
        "sun",
        "hill",
        "tree",
        "road",
    )


def test_merges_do_not_chain() -> None:
    """ "b" is merged into "a", so a merge into "b" has nothing left to stand on."""
    tags = ("a", "b", "c", "d", "e", "f")

    assert select(tags, verdict({}, ("a", "b"), ("b", "c"))) == ("a", "c", "d", "e", "f")


def test_low_probability_tags_are_dropped() -> None:
    probabilities = {"cat": 0.03, "beach": 0.04, "house": 0.98, "sun": 0.97}

    assert select(SEVEN, verdict(probabilities)) == ("house", "sun", "hill", "tree", "road")


def test_a_tag_exactly_at_the_threshold_stays() -> None:
    probabilities = {"cat": MIN_PROBABILITY, "beach": 0.49}

    assert select(SEVEN, verdict(probabilities)) == SEVEN[:-1]


def test_a_tag_without_a_score_stays() -> None:
    assert select(SEVEN, verdict({"cat": 0.01})) == (*SEVEN[:5], "beach")


# --- select_tags: the 5-10 range ---------------------------------------------------------------


def test_the_floor_is_refilled_with_the_highest_scored_tags() -> None:
    probabilities = {"house": 0.9, "sun": 0.1, "hill": 0.4, "tree": 0.3, "road": 0.2, "cat": 0.8}
    tags = ("house", "sun", "hill", "tree", "road", "cat")

    # Only "house" and "cat" pass. The three best of the rest come back: hill, tree and road.
    assert select(tags, verdict(probabilities)) == ("house", "hill", "tree", "road", "cat")


def test_refilled_tags_return_to_their_original_position() -> None:
    probabilities = {"a": 0.9, "b": 0.1, "c": 0.9, "d": 0.2, "e": 0.9, "f": 0.9}

    # "d" is the best of the two rejected tags. It comes back between "c" and "e", not at the end.
    assert select(("a", "b", "c", "d", "e", "f"), verdict(probabilities)) == (
        "a",
        "c",
        "d",
        "e",
        "f",
    )


def test_equal_scores_are_refilled_in_the_analyzers_order() -> None:
    probabilities = {"a": 0.9, "b": 0.9, "c": 0.9, "d": 0.9, "e": 0.1, "f": 0.1, "g": 0.1}

    assert select(("a", "b", "c", "d", "e", "f", "g"), verdict(probabilities)) == (
        "a",
        "b",
        "c",
        "d",
        "e",
    )


def test_the_floor_is_always_met_even_if_the_verifier_rejects_everything() -> None:
    probabilities = {tag: 0.01 * (index + 1) for index, tag in enumerate(SEVEN)}

    result = select(SEVEN, verdict(probabilities))

    assert len(result) == MIN_TAGS
    # The five best-scored: beach, cat, road, tree, hill, in the analyzer's order.
    assert result == ("hill", "tree", "road", "cat", "beach")


def test_a_merged_away_duplicate_returns_before_a_rejected_tag() -> None:
    tags = ("house", "red house", "cat", "sun", "hill", "tree")
    probabilities = {
        "house": 0.97,
        "red house": 0.98,
        "cat": 0.03,
        "sun": 0.97,
        "hill": 0.97,
        "tree": 0.97,
    }

    # The merge and the rejected cat leave four tags. A true duplicate beats a wrong tag.
    assert select(tags, verdict(probabilities, ("red house", "house"))) == (
        "house",
        "red house",
        "sun",
        "hill",
        "tree",
    )


def test_a_merge_that_would_break_the_floor_is_undone() -> None:
    tags = ("house", "red house", "sun", "hill", "tree")

    result = select(tags, verdict({}, ("red house", "house")))

    assert result == tags
    assert len(result) == MIN_TAGS


def test_fewer_tags_than_the_floor_are_all_returned() -> None:
    tags = ("house", "sun", "cat")

    assert select(tags, verdict({"cat": 0.01})) == tags


def test_the_cap_keeps_the_first_tags() -> None:
    tags = tuple(f"tag{index}" for index in range(12))

    assert select(tags, verdict()) == tags[:MAX_TAGS]


def test_the_cap_counts_only_the_tags_that_survive() -> None:
    tags = tuple(f"tag{index}" for index in range(12))

    assert select(tags, verdict({"tag0": 0.1, "tag1": 0.1})) == tags[2:12]


def test_the_range_can_be_changed() -> None:
    tags = tuple(f"tag{index}" for index in range(8))
    probabilities = {tag: 0.1 for tag in tags}

    assert select(tags, verdict(probabilities), min_count=2, max_count=3) == tags[:2]
    assert select(tags, verdict(), min_count=2, max_count=3) == tags[:3]


def test_the_original_order_is_kept() -> None:
    tags = ("zebra", "apple", "mango", "kiwi", "lemon", "plum")
    # The scores rise along the list, so a result sorted by score would come back reversed.
    probabilities = dict(zip(tags, (0.55, 0.6, 0.7, 0.8, 0.9, 0.99), strict=True))

    assert select(tags, verdict(probabilities)) == tags


def test_repeated_tags_count_once() -> None:
    assert select(("a", "a", "b", "c", "d", "e"), verdict()) == ("a", "b", "c", "d", "e")


def test_the_result_has_five_to_ten_tags_for_any_verdict() -> None:
    tags = tuple(f"tag{index}" for index in range(10))
    for score in (0.0, 0.2, 0.5, 0.9, 1.0):
        probabilities = {tag: score for tag in tags}
        merges = tuple((tags[index], tags[index + 1]) for index in range(0, 10, 2))

        result = select(tags, verdict(probabilities, *merges))

        assert MIN_TAGS <= len(result) <= MAX_TAGS


# --- duplicate_candidates ----------------------------------------------------------------------


def test_a_tag_and_its_longer_form_are_a_pair() -> None:
    assert duplicate_candidates(["house", "red house", "tree"]) == (("house", "red house"),)


def test_sun_and_yellow_sun_are_a_pair() -> None:
    assert duplicate_candidates(["sky", "sun", "yellow sun"]) == (("sun", "yellow sun"),)


def test_unrelated_tags_make_no_pair() -> None:
    assert duplicate_candidates(["house", "sun", "hill", "red car", "blue sky"]) == ()


def test_sharing_only_some_words_is_not_enough() -> None:
    assert duplicate_candidates(["red house", "red car"]) == ()


def test_the_same_words_in_another_order_are_a_pair() -> None:
    assert duplicate_candidates(["sea blue", "blue sea"]) == (("sea blue", "blue sea"),)


def test_a_pair_lists_the_tag_that_comes_first_first() -> None:
    assert duplicate_candidates(["red house", "house"]) == (("red house", "house"),)


def test_a_tag_joins_at_most_one_pair() -> None:
    tags = ["house", "red house", "big red house"]

    assert duplicate_candidates(tags) == (("house", "red house"),)


def test_at_most_three_pairs_are_asked() -> None:
    tags = [word for pair in ("a", "b", "c", "d", "e") for word in (pair, f"big {pair}")]

    pairs = duplicate_candidates(tags)

    assert MAX_MERGE_QUESTIONS == 3
    assert pairs == (("a", "big a"), ("b", "big b"), ("c", "big c"))


def test_the_cap_can_be_changed() -> None:
    tags = ["a", "big a", "b", "big b"]

    assert duplicate_candidates(tags, limit=1) == (("a", "big a"),)
    assert duplicate_candidates(tags, limit=0) == ()


@pytest.mark.parametrize("tags", [[], ["house"], ["house", "house"]])
def test_fewer_than_two_distinct_tags_make_no_pair(tags: list[str]) -> None:
    assert duplicate_candidates(tags) == ()
