# mypy: ignore-errors

"""Coupon application tests (v2 chain fixture)."""

from decimal import Decimal

import pytest
from shop.coupons import Coupon, CouponError, apply_coupon


def test_apply_coupon() -> None:
    assert apply_coupon(Decimal("100"), Coupon("spring", Decimal("10"))) == Decimal("10.00")


def test_minimum_not_met_raises() -> None:
    coupon = Coupon("spring", Decimal("10"), min_subtotal=Decimal("100"))
    with pytest.raises(CouponError):
        apply_coupon(Decimal("50"), coupon)
