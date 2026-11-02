"""Shipped on/off defaults, i.e. what a user gets before running ``cfxd set``.

Changing a default is a product decision: update this table, the docstring in
``cfxd/config.py`` and the defaults sentence in ``docs/TESTING-GUIDE.md`` together.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from cfxd.cli import _build_parser
from cfxd.config import read_config

REPO = Path(__file__).resolve().parents[1]

# brief, brief_policy and carry are on so the alpha produces data; fold/fold_read are off
# (2026-10-24 A/Bs); brief_v1 and carry_back are opt-in until the v4 pilot.
_EXPECTED = {
    "brief": True,
    "brief_policy": True,
    "carry": True,
    "fold": False,
    "fold_read": False,
    "bound": False,
    "coach": False,
    "coach_control": True,
    "idle_notice": False,
    "brief_v1": False,
    "carry_back": False,
    "carry_v2": False,
    "brief_excerpts": False,
}

# ADR-0013: retired, kept only so old configs load and `cfxd set` can print the notice.
_RETIRED = ("fold", "fold_read", "bound", "checkpoint")


def _retired_mentions(text: str) -> set[str]:
    return {name for name in _RETIRED if re.search(rf"\b{name}\b", text)}


def test_every_shipped_default_is_exact(tmp_path: Path) -> None:
    # No config.json in tmp_path, so this is the shipped default (not user state).
    assert read_config(tmp_path) == _EXPECTED


def test_opt_in_flags_default_off(tmp_path: Path) -> None:
    config = read_config(tmp_path)
    assert config["carry_v2"] is False
    assert config["brief_excerpts"] is False


def test_top_level_help_names_no_retired_feature() -> None:
    assert _retired_mentions(_build_parser().format_help()) == set()


def test_subcommand_help_names_no_retired_feature() -> None:
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser] = next(
        action for action in _build_parser()._actions if action.dest == "command"
    )
    for name, sub in subcommands.choices.items():
        if name in (*_RETIRED, "set"):
            # The retired command itself only prints the notice; `set` keeps the retired
            # names callable (and therefore in its choices list) so the notice prints.
            continue
        assert _retired_mentions(sub.format_help()) == set(), name


def test_tour_names_no_retired_feature() -> None:
    # Every tour sentence lives in this module; scanning it covers the steps that would
    # otherwise need a live daemon (the doctor step).
    assert _retired_mentions((REPO / "cfxd" / "tour.py").read_text(encoding="utf-8")) == set()


def test_tester_docs_mention_retired_features_only_to_retire_them() -> None:
    docs = [*(REPO / "docs" / "testers").glob("*.md"), REPO / "docs" / "TESTING-GUIDE.md"]
    for path in docs:
        for line in path.read_text(encoding="utf-8").splitlines():
            if _retired_mentions(line):
                assert "retired" in line, f"{path.name}: {line.strip()}"


def test_share_output_defaults_to_numeric_only() -> None:
    parser = _build_parser()
    for command in ("week", "handoff-stats", "diet"):
        args = parser.parse_args([command])
        assert args.share is False, command
        assert getattr(args, "share_digests", False) is False, command
