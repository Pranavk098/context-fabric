import click
from click.testing import CliRunner


def _group():
    @click.group()
    def cli():
        pass

    @cli.command("list", aliases=["ls", "li"])
    def list_():
        click.echo("listed")

    @cli.command()
    def other():
        click.echo("other")

    return cli


def test_alias_invokes_the_same_command():
    result = CliRunner().invoke(_group(), ["ls"])
    assert result.exit_code == 0, result.output
    assert "listed" in result.output


def test_second_alias_and_real_name_work():
    runner = CliRunner()
    assert "listed" in runner.invoke(_group(), ["li"]).output
    assert "listed" in runner.invoke(_group(), ["list"]).output
    assert "other" in runner.invoke(_group(), ["other"]).output


def test_aliases_are_not_registered_as_separate_commands():
    cli = _group()
    assert "ls" not in cli.commands
    assert "li" not in cli.commands
    assert cli.commands["list"].aliases == ("ls", "li")


def test_help_lists_the_command_once():
    result = CliRunner().invoke(_group(), ["--help"])
    assert result.exit_code == 0
    assert result.output.count("list") == 1
