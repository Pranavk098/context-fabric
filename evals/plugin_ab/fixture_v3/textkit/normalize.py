# mypy: ignore-errors

"""Text normalization for the v3 text-processing fixture."""

from __future__ import annotations

import string


def normalize(text: str) -> str:
    """Lower-case ``text`` and collapse runs of whitespace to single spaces."""
    raise NotImplementedError


def normalized_tokens(text: str) -> list[str]:
    """Split normalized ``text`` into words, dropping punctuation."""
    raise NotImplementedError
