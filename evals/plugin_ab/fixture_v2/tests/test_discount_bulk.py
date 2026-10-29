# mypy: ignore-errors

"""Bulk-discount tests (v2 chain fixture)."""

from decimal import Decimal

from shop.discounts import bulk_discount


def test_bulk_discount_threshold() -> None:
    assert bulk_discount(Decimal("99.99")) == Decimal("0")
    assert bulk_discount(Decimal("100")) == Decimal("5.00")
