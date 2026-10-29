# mypy: ignore-errors

"""Normalization tests (v3 fixture)."""

from textkit.normalize import normalize


def test_collapses_whitespace() -> None:
    assert normalize("  Hello   World  ") == "hello world"


def test_collapses_tabs() -> None:
    assert normalize("A\tB") == "a b"
