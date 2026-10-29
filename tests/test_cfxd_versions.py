"""One version everywhere: pyproject, the cfxd package and the plugin manifests must agree."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import cfxd

ROOT = Path(__file__).resolve().parents[1]


def _pep440(version: str) -> str:
    """Plugin manifests use semver (0.1.1-alpha.1); Python uses PEP 440 (0.1.1a1)."""
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:-(alpha|beta|rc)\.(\d+))?", version)
    assert match, f"unexpected version format: {version}"
    base, tag, num = match.groups()
    if tag is None:
        return base
    return f"{base}{ {'alpha': 'a', 'beta': 'b', 'rc': 'rc'}[tag] }{num}"


def test_versions_agree() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads(
        (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    assert project["version"] == _pep440(plugin["version"])
    assert cfxd.__version__ == project["version"]
    for entry in marketplace["plugins"]:
        assert entry["version"] == plugin["version"], f"marketplace entry {entry['name']} drifts"
