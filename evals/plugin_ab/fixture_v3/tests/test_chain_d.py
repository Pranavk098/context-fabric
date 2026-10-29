# mypy: ignore-errors

"""Chain D oracle: work follows the rename, the old path stays gone."""

from pathlib import Path

from textkit.lexer import tokenize, tokenize_all


def test_old_path_is_not_recreated() -> None:
    assert not Path("textkit/tokenizer.py").exists()


def test_tokenize_all() -> None:
    assert tokenize_all("One two\n\nThree") == [["one", "two"], ["three"]]
