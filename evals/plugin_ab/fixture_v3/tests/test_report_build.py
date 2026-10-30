# mypy: ignore-errors

"""Report-building tests (v3 fixture)."""

from textkit.report import build_report


def test_totals() -> None:
    report = build_report({"a": 2, "b": 1}, [("a", 2), ("b", 1)])
    assert report == {"total_words": 3, "unique_words": 2, "top": [("a", 2), ("b", 1)]}
