# mypy: ignore-errors

"""Top-word tests (v3 fixture)."""

from textkit.counts import top_words


def test_ordered_by_count_then_name() -> None:
    assert top_words(["b", "a", "a", "c", "b", "a"]) == [("a", 3), ("b", 2), ("c", 1)]


def test_respects_n_and_ties() -> None:
    assert top_words(["b", "a"], n=1) == [("a", 1)]
