# mypy: ignore-errors

"""Pricing tests (toy fixture, exercised by pytest, not the harness's typed surface)."""

from decimal import Decimal

from shop.pricing import compute_tax, format_total, total_price


def test_compute_tax() -> None:
    assert compute_tax(Decimal("10.00"), Decimal("0.05")) == Decimal("0.50")


def test_total_price() -> None:
    assert total_price(Decimal("100"), Decimal("0.05"), Decimal("10")) == Decimal("94.50")


def test_format_total() -> None:
    assert format_total(Decimal("1.5")) == "$1.50"
