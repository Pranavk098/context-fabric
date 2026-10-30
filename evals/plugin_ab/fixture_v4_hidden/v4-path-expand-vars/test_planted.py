import pytest

import click


def test_expand_path_vars_helper():
    from click.paths import expand_path_vars

    assert expand_path_vars("$CF_V4_X", True) == "$CF_V4_X"


def test_default_expand_vars_constant():
    from click.paths import DEFAULT_EXPAND_VARS

    assert DEFAULT_EXPAND_VARS is False


def test_expand_vars_type_message():
    with pytest.raises(TypeError, match="expand_vars must be a bool"):
        click.Path(expand_vars="yes").convert("x", None, None)
