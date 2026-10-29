# mypy: ignore-errors

"""Formatting tests (toy fixture, exercised by pytest, not the harness's typed surface)."""

from decimal import Decimal

from shop.formatting import format_money, format_receipt


def test_format_money() -> None:
    assert format_money(Decimal("12.3")) == "$12.30"


def test_format_receipt() -> None:
    assert format_receipt(Decimal("11.32"), Decimal("1.02")) == "Total: $12.34 (tax $1.02)"
