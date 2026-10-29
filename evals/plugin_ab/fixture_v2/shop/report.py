# mypy: ignore-errors

"""Checkout report: subtotal, discount, tax and total."""

from __future__ import annotations

from decimal import Decimal


def build_report(subtotal: Decimal, tax_rate: Decimal, discount: Decimal) -> dict[str, Decimal]:
    """Return subtotal, discount, tax and total for a checkout."""
    raise NotImplementedError


def format_report(report: dict[str, Decimal]) -> str:
    """Return a one-line report like ``Total: $12.34 (tax $1.02)``."""
    raise NotImplementedError
