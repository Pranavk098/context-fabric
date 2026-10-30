import pytest

import click


def test_build_alias_map_helper():
    from click.choice_set import build_alias_map

    assert build_alias_map(["a", "b"], {"alpha": "a"}) == {"alpha": "a"}


def test_alias_map_name_constant():
    from click.choice_set import ALIAS_MAP_NAME

    assert ALIAS_MAP_NAME == "aliases"


def test_alias_conflict_message():
    with pytest.raises(ValueError, match="alias 'a' conflicts with a choice"):
        click.Choice(["a"], aliases={"a": "a"})
