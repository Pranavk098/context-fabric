# mypy: ignore-errors

"""Money and receipt formatting."""

from __future__ import annotations

from decimal import Decimal


def format_money(amount: Decimal) -> str:
    """Format ``amount`` as US dollars with two decimals, e.g. ``$12.34``."""
    return f"${amount:.2f}"


def format_receipt(subtotal: Decimal, tax: Decimal) -> str:
    """Return a one-line receipt like ``Total: $12.34 (tax $1.02)``."""
    total = subtotal + tax
    return f"Total: {format_money(total)} (tax {format_money(tax)})"
