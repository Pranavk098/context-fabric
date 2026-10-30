import click


def _choice():
    return click.Choice(["a", "b"], aliases={"alpha": "a"}, hidden=["b"])


def test_alias_converts_to_target_choice():
    assert _choice().convert("alpha", None, None) == "a"


def test_hidden_choice_is_still_valid():
    assert _choice().convert("b", None, None) == "b"


def test_hidden_choice_is_not_in_the_metavar():
    choice = _choice()
    option = click.Option(["--pick"], type=choice)
    assert "b" not in choice.get_metavar(option, None)


def test_hidden_choice_is_not_completed():
    choice = _choice()
    option = click.Option(["--pick"], type=choice)
    ctx = click.Context(click.Command("cli"))
    values = [item.value for item in choice.shell_complete(ctx, option, "")]
    assert "b" not in values


def test_hidden_choice_is_not_in_the_invalid_message():
    assert "b" not in _choice().get_invalid_choice_message("z", None)
