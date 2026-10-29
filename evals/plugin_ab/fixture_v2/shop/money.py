# mypy: ignore-errors

"""Money helpers shared by the v2 chain tasks."""

from __future__ import annotations

from decimal import Decimal


def round_cents(amount: Decimal) -> Decimal:
    """Round ``amount`` to cents, half away from zero."""
    raise NotImplementedError
