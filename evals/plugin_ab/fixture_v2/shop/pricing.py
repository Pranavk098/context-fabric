# mypy: ignore-errors

"""Pricing: tax, totals and money formatting."""

from __future__ import annotations

from decimal import Decimal

from shop.formatting import format_money


def compute_tax(subtotal: Decimal, rate: Decimal) -> Decimal:
    """Return the sales tax on ``subtotal`` at ``rate`` (e.g. 0.0825), rounded to cents."""
    raise NotImplementedError


def total_price(subtotal: Decimal, tax_rate: Decimal, discount: Decimal) -> Decimal:
    """Return the final total: discount applied, then tax, rounded to cents."""
    raise NotImplementedError


def format_total(total: Decimal) -> str:
    """Format a money amount for display."""
    return format_money(total)
