# mypy: ignore-errors

"""Total-price tests (v2 chain fixture)."""

from decimal import Decimal

from shop.pricing import total_price


def test_total_price_after_discount() -> None:
    assert total_price(Decimal("100"), Decimal("0.05"), Decimal("10")) == Decimal("94.50")
