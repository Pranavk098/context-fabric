import click


def test_open_file_forces_crlf(tmp_path):
    path = tmp_path / "crlf.txt"
    with click.open_file(str(path), "w", encoding="utf-8", newline="\r\n") as f:
        f.write("a\nb")
    assert path.read_bytes() == b"a\r\nb"


def test_lazy_open_file_forces_crlf(tmp_path):
    path = tmp_path / "lazy.txt"
    with click.open_file(str(path), "w", encoding="utf-8", newline="\r\n", lazy=True) as f:
        f.write("a\nb")
    assert path.read_bytes() == b"a\r\nb"


def test_empty_newline_disables_translation(tmp_path):
    path = tmp_path / "raw.txt"
    with click.open_file(str(path), "w", encoding="utf-8", newline="") as f:
        f.write("a\nb")
    assert path.read_bytes() == b"a\nb"


def test_default_open_file_is_unchanged(tmp_path):
    path = tmp_path / "default.txt"
    with click.open_file(str(path), "w", encoding="utf-8") as f:
        f.write("a\nb")
    assert path.read_text(encoding="utf-8") == "a\nb"
