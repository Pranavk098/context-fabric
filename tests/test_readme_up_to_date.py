"""Guards that the README keeps up with the code (a new command cannot ship undocumented)."""

from __future__ import annotations

import argparse
from pathlib import Path

from cfxd.cli import _build_parser

ROOT = Path(__file__).resolve().parent.parent
# Internal plumbing that testers never run by hand.
_INTERNAL = {"serve", "stop", "status"}


def _mentioned(text: str, cmd: str) -> bool:
    return f"cfxd {cmd}" in text or f"`{cmd}`" in text or f"`{cmd} " in text


def _subcommands() -> set[str]:
    parser = _build_parser()
    for action in parser._actions:  # noqa: SLF001 - argparse has no public listing
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return set(action.choices)
    return set()


def test_every_user_command_is_in_the_readme() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    missing = sorted(cmd for cmd in _subcommands() - _INTERNAL if not _mentioned(readme, cmd))
    assert not missing, f"commands missing from README.md: {missing}"


def test_every_user_command_is_in_the_install_guide() -> None:
    guide = (ROOT / "docs" / "INSTALL-ALPHA.md").read_text(encoding="utf-8")
    missing = sorted(cmd for cmd in _subcommands() - _INTERNAL if not _mentioned(guide, cmd))
    assert not missing, f"commands missing from docs/INSTALL-ALPHA.md: {missing}"


def test_m8ven_badge_is_one_intact_line() -> None:
    lines = [
        line
        for line in (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
        if "m8ven.ai/badge" in line
    ]
    assert len(lines) == 1
    assert lines[0].startswith("[![M8ven Score](https://m8ven.ai/badge/mcp/")
    assert lines[0].endswith("?s=readme)")
