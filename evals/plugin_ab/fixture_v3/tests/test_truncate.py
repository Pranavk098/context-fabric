# mypy: ignore-errors

"""Truncation tests (v3 fixture; reads the project limit at call time)."""

from textkit.tokenizer import truncate


def test_uses_the_current_project_limit() -> None:
    assert len(truncate([f"w{i}" for i in range(600)])) == 500


def test_short_input_untouched() -> None:
    assert truncate(["a", "b", "c"]) == ["a", "b", "c"]


def test_explicit_limit_wins() -> None:
    assert len(truncate([f"w{i}" for i in range(600)], 7)) == 7
