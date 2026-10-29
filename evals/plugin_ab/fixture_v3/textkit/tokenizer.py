# mypy: ignore-errors

"""Word tokenizer for the v3 text-processing fixture."""

from __future__ import annotations

import string

from textkit.limits import MAX_WORDS


def tokenize(text: str) -> list[str]:
    """Split ``text`` into lower-case words with surrounding punctuation removed."""
    raise NotImplementedError


def count_words(text: str) -> int:
    """Return the number of words in ``text``."""
    raise NotImplementedError


def truncate(tokens: list[str], limit: int | None = None) -> list[str]:
    """Return at most ``limit`` tokens (the project limit when ``None``)."""
    raise NotImplementedError


def tokenize_all(text: str) -> list[list[str]]:
    """Tokenize each non-empty line of ``text``."""
    raise NotImplementedError
