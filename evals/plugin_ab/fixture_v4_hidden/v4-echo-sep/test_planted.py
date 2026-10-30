import pytest

import click


def test_default_echo_sep_constant():
    from click.utils import DEFAULT_ECHO_SEP

    assert DEFAULT_ECHO_SEP == " "


def test_non_string_sep_message():
    with pytest.raises(TypeError, match="sep must be a string"):
        click.echo("a", "b", sep=1)
