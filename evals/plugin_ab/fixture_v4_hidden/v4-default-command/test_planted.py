import click
from click.testing import CliRunner


def test_pick_default_command_helper():
    from click.defaults import pick_default_command

    assert pick_default_command("status", []) == "status"
    assert pick_default_command("status", ["deploy"]) is None
    assert pick_default_command(None, []) is None


def test_default_command_attr_constant():
    from click.defaults import DEFAULT_COMMAND_ATTR

    assert DEFAULT_COMMAND_ATTR == "default_command"


def test_missing_default_command_message():
    @click.group(default_command="missing")
    def cli():
        pass

    result = CliRunner().invoke(cli, [])
    assert isinstance(result.exception, RuntimeError)
    assert str(result.exception) == "Default command 'missing' is not registered."
