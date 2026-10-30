import click
from click.testing import CliRunner


def _run(body):
    @click.command()
    def cli():
        body()

    return CliRunner().invoke(cli, [])


def test_echo_joins_values_with_sep():
    result = _run(lambda: click.echo("a", "b", "c", sep="-"))
    assert result.exit_code == 0
    assert result.output == "a-b-c\n"


def test_echo_default_separator_is_a_space():
    result = _run(lambda: click.echo("a", "b"))
    assert result.output == "a b\n"


def test_echo_single_message_and_none_are_unchanged():
    assert _run(lambda: click.echo("solo")).output == "solo\n"
    assert _run(lambda: click.echo(None)).output == "\n"
    assert _run(lambda: click.echo(message="kw")).output == "kw\n"


def test_secho_forwards_sep():
    result = _run(lambda: click.secho("a", "b", sep="+", color=False))
    assert result.exit_code == 0
    assert result.output == "a+b\n"
