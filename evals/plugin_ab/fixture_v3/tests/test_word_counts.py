# mypy: ignore-errors

"""Frequency-table tests (v3 fixture)."""

from textkit.counts import word_counts


def test_counts_each_token() -> None:
    assert word_counts(["a", "b", "a"]) == {"a": 2, "b": 1}


def test_empty_list() -> None:
    assert word_counts([]) == {}
