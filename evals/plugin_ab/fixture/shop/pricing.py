# mypy: ignore-errors

"""Pricing: tax, totals and money formatting."""

from __future__ import annotations

from decimal import Decimal

from shop.formatting import format_money


def compute_tax(subtotal: Decimal, rate: Decimal) -> Decimal:
    """Return the sales tax on ``subtotal`` at ``rate`` (e.g. 0.0825), rounded to cents."""
    return round(subtotal * rate, 2)


def total_price(subtotal: Decimal, tax_rate: Decimal, discount: Decimal) -> Decimal:
    """Return the final total: discount applied, then tax, rounded to cents."""
    discounted = subtotal - discount
    return round(discounted + compute_tax(discounted, tax_rate), 2)


def format_total(total: Decimal) -> str:
    """Format a money amount for display."""
    return format_money(total)
