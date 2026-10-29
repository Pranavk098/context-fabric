"""Tests for scripts/opencode_budget.py: real `opencode stats` shape, verdicts, exit codes."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "opencode_budget.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("opencode_budget", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOD = _load()

TODAY = """today · all projects

COST & TOKENS
  cost                $0.74
  input               1.6m
  output              261.9k
  cache read          56.9m
  cached input        97.2%

MODELS
model                                 tokens       steps        cost
opencode-go/deepseek-v4.1-flash#h…     59.1m         840       $0.74
"""

WEEK = """last 7 days · all projects

COST & TOKENS
  cost                $12.40
  input               21.4m
  output              3.1m

MODELS
model                                 tokens       steps        cost
opencode-go/deepseek-v4.1-flash#h…    208.2m        4121      $12.40
"""


def test_parse_cost_reads_both_windows() -> None:
    assert MOD.parse_cost(TODAY) == 0.74
    assert MOD.parse_cost(WEEK) == 12.40


def test_parse_cost_missing_is_none() -> None:
    assert MOD.parse_cost("today · all projects\n\nNo usage.\n") is None


@pytest.mark.parametrize(
    ("day", "week", "expected"),
    [
        (0.0, 0.0, (0, "OK")),
        (5.0, 0.0, (1, "WARN")),
        (0.0, 15.0, (1, "WARN")),
        (10.0, 0.0, (2, "STOP")),
        (0.0, 30.0, (2, "STOP")),
        (10.0, 40.0, (2, "STOP")),
    ],
)
def test_verdicts(day: float, week: float, expected: tuple[int, str]) -> None:
    assert MOD.verdict(day, week) == expected


def test_thresholds_are_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_BUDGET_WARN_DAY", "1")
    monkeypatch.setenv("OPENCODE_BUDGET_STOP_WEEK", "100")
    assert MOD.verdict(1.5, 2.0) == (1, "WARN")


def test_missing_binary_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(MOD.shutil, "which", lambda _name: None)
    assert MOD.main() == 0
    assert capsys.readouterr().out.strip() == "opencode not found"
