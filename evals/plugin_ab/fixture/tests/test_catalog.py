# mypy: ignore-errors

"""Catalog tests (toy fixture, exercised by pytest, not the harness's typed surface)."""

from decimal import Decimal

import pytest
from shop.catalog import get_product, list_products


def test_get_product() -> None:
    assert get_product("milk").unit_price == Decimal("2.99")


def test_unknown_product() -> None:
    with pytest.raises(KeyError):
        get_product("nope")


def test_list_products() -> None:
    assert len(list_products()) == 5
