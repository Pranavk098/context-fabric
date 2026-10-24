"""cfxd is packaged with a `cfxd` command, so `uvx --from <repo> cfxd ...` needs no clone."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_cfxd_is_packaged_and_has_a_script() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "cfxd" in data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert data["project"]["scripts"]["cfxd"] == "cfxd.cli:main"
