# mypy: ignore-errors

"""Refunds: restocking fees and caps."""

from __future__ import annotations

from decimal import Decimal

# RefundError is created in the refund-amount step.


def refund_amount(
    order_total: Decimal,
    returned_total: Decimal,
    restocking_percent: Decimal = Decimal("0"),
) -> Decimal:
    """Return the refund for ``returned_total`` after a restocking fee, or raise RefundError."""
    raise NotImplementedError


def capped_refund(amount: Decimal, cap: Decimal) -> Decimal:
    """Return ``amount`` capped at ``cap``, or raise RefundError."""
    raise NotImplementedError
