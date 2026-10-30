import pytest

import click


def test_default_newline_constant():
    from click._compat import DEFAULT_NEWLINE

    assert DEFAULT_NEWLINE is None


def test_validate_newline_helper():
    from click._compat import validate_newline

    assert validate_newline("\r\n") == "\r\n"
    assert validate_newline(None) is None


def test_invalid_newline_message(tmp_path):
    with pytest.raises(
        ValueError, match=r"newline must be None, '', '\\n', '\\r', or '\\r\\n'"
    ):
        click.open_file(str(tmp_path / "x.txt"), "w", newline="bad")
