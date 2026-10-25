# mypy: ignore-errors

"""Inventory: stock levels, reserve and release."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Inventory:
    stock: dict[str, int] = field(default_factory=dict)

    def add(self, product_id: str, quantity: int) -> None:
        self.stock[product_id] = self.stock.get(product_id, 0) + quantity

    def available(self, product_id: str) -> int:
        return self.stock.get(product_id, 0)

    def reserve(self, product_id: str, quantity: int) -> bool:
        """Reserve ``quantity`` of ``product_id`` if in stock; return success."""
        if quantity <= 0:
            return False
        if self.stock.get(product_id, 0) < quantity:
            return False
        self.stock[product_id] -= quantity
        return True

    def release(self, product_id: str, quantity: int) -> None:
        self.stock[product_id] = self.stock.get(product_id, 0) + quantity
