# mypy: ignore-errors

"""Rounding helper tests (v2 chain fixture)."""

from decimal import Decimal

from shop.money import round_cents


def test_half_up_rounding() -> None:
    assert round_cents(Decimal("1.005")) == Decimal("1.01")
    assert round_cents(Decimal("1.004")) == Decimal("1.00")
