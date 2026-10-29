# mypy: ignore-errors

"""Tax tests (v2 chain fixture)."""

from decimal import Decimal

from shop.pricing import compute_tax


def test_compute_tax() -> None:
    assert compute_tax(Decimal("10.00"), Decimal("0.05")) == Decimal("0.50")
