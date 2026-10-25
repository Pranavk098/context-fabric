# mypy: ignore-errors

"""Discount rules."""

from __future__ import annotations

from decimal import Decimal


def apply_discount(subtotal: Decimal, rate: Decimal) -> Decimal:
    """Return the discount amount for ``subtotal`` at ``rate`` (between 0 and 1)."""
    if rate < Decimal("0") or rate > Decimal("1"):
        raise ValueError("rate must be between 0 and 1")
    return round(subtotal * rate, 2)


def bulk_discount(subtotal: Decimal) -> Decimal:
    """Return 5% of ``subtotal`` when it is at least $100, otherwise zero."""
    if subtotal >= Decimal("100"):
        return apply_discount(subtotal, Decimal("0.05"))
    return Decimal("0")
