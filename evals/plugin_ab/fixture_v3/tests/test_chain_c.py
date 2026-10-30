# mypy: ignore-errors

"""Chain C oracle: the reverted normalization is back and extended."""

from textkit.normalize import normalize, normalized_tokens


def test_normalize_is_back() -> None:
    assert normalize("  Hello   World  ") == "hello world"


def test_normalized_tokens_drop_punctuation() -> None:
    assert normalized_tokens("Hi, There!") == ["hi", "there"]
