import pytest

import click
from click.testing import CliRunner


def test_find_alias_helper_exists():
    from click.aliases import find_alias

    @click.group()
    def cli():
        pass

    @cli.command("list", aliases=["ls"])
    def list_():
        pass

    assert find_alias(cli.commands, "ls") is cli.commands["list"]


def test_alias_help_separator():
    from click.aliases import ALIAS_HELP_SEPARATOR

    assert ALIAS_HELP_SEPARATOR == " / "


def test_alias_help_row():
    @click.group()
    def cli():
        pass

    @cli.command("list", aliases=["ls", "li"])
    def list_():
        pass

    output = CliRunner().invoke(cli, ["--help"]).output
    assert "(aliases: ls / li)" in output


def test_empty_alias_message():
    with pytest.raises(ValueError, match="aliases must be non-empty strings"):
        click.Command("broken", aliases=[""])
