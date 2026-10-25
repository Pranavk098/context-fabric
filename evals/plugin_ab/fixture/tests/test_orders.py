# mypy: ignore-errors

"""Order subtotal tests (toy fixture, exercised by pytest, not the harness's typed surface)."""

from decimal import Decimal

from shop.orders import Order


def test_empty_order_subtotal() -> None:
    assert Order().subtotal() == Decimal("0")


def test_subtotal_sums_lines() -> None:
    order = Order()
    order.add("apple", 2)
    order.add("milk", 1)
    assert order.subtotal() == Decimal("3.99")


def test_formatted_subtotal() -> None:
    order = Order()
    order.add("apple", 2)
    assert order.formatted_subtotal() == "$1.00"
