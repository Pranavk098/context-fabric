# mypy: ignore-errors

"""Refund-cap tests (v2 chain fixture)."""

from decimal import Decimal

import pytest
from shop.refunds import RefundError, capped_refund


def test_capped_refund() -> None:
    assert capped_refund(Decimal("50"), Decimal("40")) == Decimal("40")
    assert capped_refund(Decimal("30"), Decimal("40")) == Decimal("30")


def test_negative_amount_raises() -> None:
    with pytest.raises(RefundError):
        capped_refund(Decimal("-1"), Decimal("40"))
