# mypy: ignore-errors

"""Product catalog: lookup products by id."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    unit_price: Decimal


_CATALOG: dict[str, Product] = {
    "apple": Product("apple", "Apple", Decimal("0.50")),
    "banana": Product("banana", "Banana", Decimal("0.30")),
    "cherry": Product("cherry", "Cherry", Decimal("0.15")),
    "milk": Product("milk", "Whole Milk", Decimal("2.99")),
    "bread": Product("bread", "Sourdough Bread", Decimal("3.49")),
}


def get_product(product_id: str) -> Product:
    """Return the product with ``product_id``, or raise ``KeyError``."""
    return _CATALOG[product_id]


def list_products() -> list[Product]:
    """Return every product in the catalog."""
    return list(_CATALOG.values())
