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
    raise NotImplementedError


def percent_discount(subtotal: Decimal, percent: Decimal) -> Decimal:
    """Return ``percent`` of ``subtotal`` rounded to cents; percent is 0-100."""
    raise NotImplementedError
