# mypy: ignore-errors

"""Coupon model tests (v2 chain fixture)."""

from decimal import Decimal

from shop.coupons import Coupon, CouponError, normalize_code


def test_normalize_code() -> None:
    assert normalize_code(" spring ") == "SPRING"


def test_error_class_is_a_value_error() -> None:
    assert issubclass(CouponError, ValueError)


def test_coupon_defaults() -> None:
    coupon = Coupon("spring", Decimal("10"))
    assert coupon.min_subtotal == Decimal("0")
    assert coupon.expires_in_days is None
