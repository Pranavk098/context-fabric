# mypy: ignore-errors

"""Summary report for the v3 text-processing fixture."""

from __future__ import annotations


def build_report(counts: dict[str, int], top: list[tuple[str, int]]) -> dict[str, object]:
    """Return total, unique and top-word fields for a summary report."""
    raise NotImplementedError


def format_report(report: dict[str, object]) -> str:
    """Format a report as ``words: 3, unique: 2, top: a (2), b (1)``."""
    raise NotImplementedError
