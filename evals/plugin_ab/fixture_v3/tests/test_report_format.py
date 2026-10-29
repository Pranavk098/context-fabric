# mypy: ignore-errors

"""Report-format tests (v3 fixture)."""

from textkit.report import format_report


def test_one_line_format() -> None:
    report = {"total_words": 3, "unique_words": 2, "top": [("a", 2), ("b", 1)]}
    assert format_report(report) == "words: 3, unique: 2, top: a (2), b (1)"
