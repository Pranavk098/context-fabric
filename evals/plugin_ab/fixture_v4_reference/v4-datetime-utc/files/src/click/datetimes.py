"""Datetime helpers."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone

DEFAULT_ASSUME_UTC = False


def validate_assume_utc(value: bool) -> bool:
    """Reject non-boolean ``assume_utc`` values."""
    if not isinstance(value, bool):
        raise TypeError("assume_utc must be a bool")
    return value


def attach_utc(value: datetime) -> datetime:
    """Attach UTC to a naive datetime; leave aware values alone."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
