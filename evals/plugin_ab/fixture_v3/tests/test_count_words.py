# mypy: ignore-errors

"""Word-count tests (v3 fixture)."""

from textkit.tokenizer import count_words


def test_counts_words() -> None:
    assert count_words("one two  three") == 3


def test_empty_text_has_no_words() -> None:
    assert count_words("") == 0
