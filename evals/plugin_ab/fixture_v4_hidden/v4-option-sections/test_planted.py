import click
from click.testing import CliRunner


def test_group_by_section_helper():
    from click.formatting import group_by_section

    grouped = group_by_section(
        [("--a", "A", None), ("--b", "B", "Network"), ("--c", "C", "Network")]
    )
    assert grouped["Options"] == [("--a", "A")]
    assert grouped["Network"] == [("--b", "B"), ("--c", "C")]


def test_default_option_section_constant():
    from click.formatting import DEFAULT_OPTION_SECTION

    assert DEFAULT_OPTION_SECTION == "Options"


def test_named_section_is_uppercase():
    @click.command()
    @click.option("--token", section="Network")
    def cli(token):
        pass

    output = CliRunner().invoke(cli, ["--help"]).output
    assert "NETWORK:" in output
