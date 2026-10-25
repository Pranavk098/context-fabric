# mypy: ignore-errors

"""Inventory tests (toy fixture, exercised by pytest, not the harness's typed surface)."""

from shop.inventory import Inventory


def test_reserve_available() -> None:
    inv = Inventory()
    inv.add("apple", 5)
    assert inv.reserve("apple", 5) is True
    assert inv.available("apple") == 0


def test_reserve_insufficient() -> None:
    inv = Inventory()
    inv.add("apple", 2)
    assert inv.reserve("apple", 3) is False
    assert inv.available("apple") == 2


def test_reserve_nonpositive() -> None:
    inv = Inventory()
    inv.add("apple", 2)
    assert inv.reserve("apple", 0) is False
    assert inv.reserve("apple", -1) is False
