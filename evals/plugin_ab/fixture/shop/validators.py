# mypy: ignore-errors

"""Order validation."""

from __future__ import annotations

from decimal import Decimal

from shop.orders import Order


def validate_order(order: Order) -> list[str]:
    """Return validation errors for ``order`` (empty list when valid)."""
    errors: list[str] = []
    for line in order.lines:
        if line.quantity <= 0:
            errors.append(f"invalid quantity {line.quantity} for {line.product_id}")
    if order.discount_rate < Decimal("0") or order.discount_rate > Decimal("1"):
        errors.append("discount rate out of range")
    return errors
