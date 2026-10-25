# mypy: ignore-errors

"""Validator tests (toy fixture, exercised by pytest, not the harness's typed surface)."""

from decimal import Decimal

from shop.orders import Order
from shop.validators import validate_order


def test_valid_order() -> None:
    order = Order()
    order.add("apple", 2)
    assert validate_order(order) == []


def test_negative_quantity() -> None:
    order = Order()
    order.add("apple", -3)
    assert validate_order(order) == ["invalid quantity -3 for apple"]


def test_bad_discount_rate() -> None:
    order = Order()
    order.add("apple", 1)
    order.discount_rate = Decimal("1.5")
    assert validate_order(order) == ["discount rate out of range"]
