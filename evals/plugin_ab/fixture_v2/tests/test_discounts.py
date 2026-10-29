# mypy: ignore-errors

"""Discount rule tests (toy fixture, exercised by pytest, not the harness's typed surface)."""

from decimal import Decimal

import pytest
from shop.discounts import apply_discount, bulk_discount


def test_apply_discount() -> None:
    assert apply_discount(Decimal("100"), Decimal("0.10")) == Decimal("10.00")


def test_bulk_discount_threshold() -> None:
    assert bulk_discount(Decimal("99.99")) == Decimal("0")
    assert bulk_discount(Decimal("100")) == Decimal("5.00")


def test_rate_out_of_range() -> None:
    with pytest.raises(ValueError):
        apply_discount(Decimal("10"), Decimal("1.5"))
