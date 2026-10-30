from datetime import datetime

import pytest

import click


def test_attach_utc_helper():
    from click.datetimes import attach_utc

    value = attach_utc(datetime(2024, 1, 2))
    assert value.tzinfo is not None


def test_default_assume_utc_constant():
    from click.datetimes import DEFAULT_ASSUME_UTC

    assert DEFAULT_ASSUME_UTC is False


def test_assume_utc_type_message():
    with pytest.raises(TypeError, match="assume_utc must be a bool"):
        click.DateTime(assume_utc="yes")
