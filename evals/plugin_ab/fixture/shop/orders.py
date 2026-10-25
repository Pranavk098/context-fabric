# mypy: ignore-errors

"""Orders: line items and subtotals."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from shop.catalog import get_product
from shop.formatting import format_money


@dataclass
class OrderLine:
    product_id: str
    quantity: int


@dataclass
class Order:
    lines: list[OrderLine] = field(default_factory=list)
    discount_rate: Decimal = Decimal("0")

    def add(self, product_id: str, quantity: int) -> None:
        self.lines.append(OrderLine(product_id, quantity))

    def subtotal(self) -> Decimal:
        total = Decimal("0")
        for line in self.lines:
            total += get_product(line.product_id).unit_price * line.quantity
        return total

    def formatted_subtotal(self) -> str:
        return format_money(self.subtotal())
