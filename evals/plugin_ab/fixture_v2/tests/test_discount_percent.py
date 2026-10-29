# mypy: ignore-errors

"""Percentage-discount tests (v2 chain fixture)."""

from decimal import Decimal

from shop.discounts import percent_discount


def test_percent_discount_rounds_half_up() -> None:
    assert percent_discount(Decimal("10.05"), Decimal("10")) == Decimal("1.01")
