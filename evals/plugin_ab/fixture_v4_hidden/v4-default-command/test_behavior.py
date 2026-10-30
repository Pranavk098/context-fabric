import click
from click.testing import CliRunner


def _group():
    @click.group(default_command="status")
    def cli():
        pass

    @cli.command()
    def status():
        click.echo("status ran")

    @cli.command()
    def deploy():
        click.echo("deploy ran")

    return cli


def test_no_args_invokes_default_command():
    result = CliRunner().invoke(_group(), [])
    assert result.exit_code == 0, result.output
    assert "status ran" in result.output


def test_named_command_still_wins():
    result = CliRunner().invoke(_group(), ["deploy"])
    assert result.exit_code == 0
    assert "deploy ran" in result.output


def test_group_without_default_still_requires_a_command():
    @click.group()
    def cli():
        pass

    result = CliRunner().invoke(cli, [])
    assert result.exit_code != 0


def test_default_command_is_listed_once():
    output = CliRunner().invoke(_group(), ["--help"]).output
    assert output.count("status") == 1
