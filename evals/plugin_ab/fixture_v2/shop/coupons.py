# mypy: ignore-errors

"""Coupons: codes, validation and discount amounts."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# CouponError is created in the coupon-model step.


@dataclass(frozen=True)
class Coupon:
    code: str
    percent: Decimal
    min_subtotal: Decimal = Decimal("0")
    expires_in_days: int | None = None


def normalize_code(code: str) -> str:
    """Return the coupon code stripped and upper-cased."""
    raise NotImplementedError


def apply_coupon(subtotal: Decimal, coupon: Coupon) -> Decimal:
    """Return the discount for ``coupon`` on ``subtotal``, or raise CouponError."""
    raise NotImplementedError


def validate_coupon(coupon: Coupon, days_since_issue: int) -> None:
    """Raise CouponError when ``coupon`` has expired."""
    raise NotImplementedError
