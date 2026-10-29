# mypy: ignore-errors

"""Refund-amount tests (v2 chain fixture)."""

from decimal import Decimal

import pytest
from shop.refunds import RefundError, refund_amount


def test_refund_after_restocking_fee() -> None:
    assert refund_amount(Decimal("100"), Decimal("50"), Decimal("10")) == Decimal("45.00")


def test_returned_total_out_of_range() -> None:
    with pytest.raises(RefundError):
        refund_amount(Decimal("100"), Decimal("150"))
