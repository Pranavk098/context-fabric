import click
from click.testing import CliRunner


def _command():
    @click.command()
    @click.option("--token", section="Network", help="API token")
    @click.option("--verbose", is_flag=True, help="Verbose output")
    def cli(token, verbose):
        pass

    return cli


def test_help_keeps_every_option():
    result = CliRunner().invoke(_command(), ["--help"])
    assert result.exit_code == 0
    assert "--token" in result.output
    assert "--verbose" in result.output
    assert "Options:" in result.output


def test_named_section_contains_its_option():
    output = CliRunner().invoke(_command(), ["--help"]).output
    assert "network" in output.lower()
    assert "API token" in output


def test_unnamed_options_stay_in_the_default_section():
    output = CliRunner().invoke(_command(), ["--help"]).output
    options_at = output.index("Options:")
    network_at = output.lower().index("network")
    assert options_at < network_at
    assert "--verbose" in output[options_at:network_at]
