from pathlib import Path

import click


def test_expand_vars_resolves_before_checks(tmp_path, monkeypatch):
    target = tmp_path / "data.txt"
    target.write_text("ok", encoding="utf-8")
    monkeypatch.setenv("CF_V4_DIR", str(tmp_path))

    path_type = click.Path(exists=True, expand_vars=True)
    result = path_type.convert("$CF_V4_DIR/data.txt", None, None)

    assert Path(result) == target


def test_expand_vars_is_off_by_default():
    path_type = click.Path()
    value = "$CF_V4_DIR/data.txt"
    assert path_type.convert(value, None, None) == value


def test_expand_vars_without_env_var_keeps_text(tmp_path):
    path_type = click.Path(expand_vars=True)
    assert path_type.convert("$CF_V4_MISSING/x", None, None) == "$CF_V4_MISSING/x"
