# mypy: ignore-errors

"""Word frequency helpers for the v3 text-processing fixture."""

from __future__ import annotations


def word_counts(tokens: list[str]) -> dict[str, int]:
    """Count each token, keeping insertion order."""
    raise NotImplementedError


def top_words(tokens: list[str], n: int = 3) -> list[tuple[str, int]]:
    """Return the ``n`` most common tokens, ties broken alphabetically."""
    raise NotImplementedError
