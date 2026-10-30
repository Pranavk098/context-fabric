from datetime import datetime, timedelta, timezone

import click


def test_assume_utc_attaches_utc_to_naive_values():
    value = click.DateTime(assume_utc=True).convert("2024-01-02", None, None)
    assert value == datetime(2024, 1, 2, tzinfo=timezone.utc)
    assert value.tzinfo is timezone.utc


def test_aware_values_are_unchanged():
    aware = datetime(2024, 1, 2, tzinfo=timezone(timedelta(hours=2)))
    assert click.DateTime(assume_utc=True).convert(aware, None, None) is aware


def test_default_stays_naive():
    value = click.DateTime().convert("2024-01-02", None, None)
    assert value.tzinfo is None
