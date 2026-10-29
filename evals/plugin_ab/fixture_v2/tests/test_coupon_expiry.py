# mypy: ignore-errors

"""Coupon expiry tests (v2 chain fixture)."""

from decimal import Decimal

import pytest
from shop.coupons import Coupon, CouponError, validate_coupon


def test_expired_coupon_raises() -> None:
    coupon = Coupon("spring", Decimal("10"), expires_in_days=30)
    with pytest.raises(CouponError):
        validate_coupon(coupon, 31)


def test_coupon_valid_on_last_day() -> None:
    coupon = Coupon("spring", Decimal("10"), expires_in_days=30)
    validate_coupon(coupon, 30)
