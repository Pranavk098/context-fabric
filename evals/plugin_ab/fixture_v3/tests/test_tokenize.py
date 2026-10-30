# mypy: ignore-errors

"""Tokenizer tests (v3 fixture)."""

from textkit.tokenizer import tokenize


def test_splits_and_lowercases() -> None:
    assert tokenize("Hello, World!") == ["hello", "world"]


def test_keeps_inner_punctuation() -> None:
    assert tokenize("don't  stop") == ["don't", "stop"]
