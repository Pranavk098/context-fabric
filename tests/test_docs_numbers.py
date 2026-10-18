"""Tests for scripts/check_docs_numbers.py — the docs headline-number drift guard."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_docs_numbers.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_docs_numbers", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOD = _load()


def _write_registry(root: Path, payload: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    registry = root / "NUMBERS.md"
    registry.write_text("```json\n" + json.dumps(payload) + "\n```\n", encoding="utf-8")
    return registry


def _run(root: Path, payload: dict) -> list[tuple[bool, str]]:
    return MOD.run(_write_registry(root, payload), root)


def _messages(report: list[tuple[bool, str]]) -> str:
    return "\n".join(message for _, message in report)


def test_evaluate_ratio_difference_and_sum() -> None:
    artifact = {"arms": {"a": 44854}, "b": 14170}
    assert MOD.evaluate({"expr": "a/b", "a": "arms.a", "b": "b"}, artifact) == 3.17
    assert MOD.evaluate({"expr": "a-b", "a": "arms.a", "b": "b"}, artifact) == 30684
    assert MOD.evaluate({"expr": "a+b", "a": "arms.a", "b": "b"}, artifact) == 59024
    assert MOD.evaluate({"expr": "a/b", "a": "arms.a", "b": "b", "round": 4}, artifact) == 3.1654


def test_evaluate_resolves_list_indices() -> None:
    artifact = {"cases": [{"v": 0.0138231}, {"v": 0.2795341}]}
    assert MOD.evaluate({"expr": "a/b", "a": "cases.1.v", "b": "cases.0.v"}, artifact) == 20.22


def test_evaluate_rejects_unknown_expression() -> None:
    with pytest.raises(ValueError, match="unsupported expression"):
        MOD.evaluate({"expr": "a*b", "a": "x", "b": "y"}, {"x": 1, "y": 2})


def test_missing_artifact_fails(tmp_path: Path) -> None:
    payload = {
        "entries": [
            {"id": "demo", "display": "1x", "artifact": "evals/reports/nope.json"},
        ]
    }
    report = _run(tmp_path, payload)
    assert any(not ok and "artifact missing" in message for ok, message in report)


def test_display_present_and_absent(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("worth 3.17\u00d7 fewer tokens\n", encoding="utf-8")
    payload = {
        "entries": [
            {
                "id": "ratio",
                "display": "3.17x",
                "must_appear_in": ["README.md"],
            },
            {
                "id": "ghost",
                "display": "9.99x",
                "must_appear_in": ["README.md"],
            },
        ]
    }
    messages = _messages(_run(tmp_path, payload))
    assert "display '3.17x' present in README.md" in messages
    assert "display '9.99x' NOT FOUND in README.md" in messages


def test_missing_doc_fails(tmp_path: Path) -> None:
    payload = {"entries": [{"id": "x", "display": "1x", "must_appear_in": ["gone.md"]}]}
    assert any(not ok and "doc missing: gone.md" in m for ok, m in _run(tmp_path, payload))


def test_normalization_matches_x_and_unicode_minus(tmp_path: Path) -> None:
    text = "20.2\u00d7 lower, \u221213.4% redundancy\n"
    (tmp_path / "README.md").write_text(text, encoding="utf-8")
    payload = {
        "entries": [
            {"id": "kv", "display": "20.2x", "must_appear_in": ["README.md"]},
            {"id": "pack", "display": "-13.4%", "must_appear_in": ["README.md"]},
        ]
    }
    assert not [m for ok, m in _run(tmp_path, payload) if not ok]


def test_stale_pattern_scoped_and_historical_exempt(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("clean\n", encoding="utf-8")
    (tmp_path / "docs" / "OLD.md").write_text("8.6\u00d7 stale\n", encoding="utf-8")
    payload = {
        "stale_patterns": [{"pattern": "8.6x", "docs": ["README.md", "docs/OLD.md"]}],
        "historical_ok": ["docs/OLD.md"],
    }
    report = _run(tmp_path, payload)
    assert not [m for ok, m in report if not ok]
    (tmp_path / "README.md").write_text("still 8.6x here\n", encoding="utf-8")
    failures = [m for ok, m in _run(tmp_path, payload) if not ok]
    assert failures == ["stale pattern '8.6x' absent from README.md"]


def test_unscoped_stale_scans_repo_but_not_registry(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text("no stale numbers\n", encoding="utf-8")
    payload = {"stale_patterns": [{"pattern": "44.8"}]}
    report = _run(tmp_path, payload)
    assert all(ok for ok, _ in report)
    (tmp_path / "docs" / "note.md").write_text("44.8 req/s\n", encoding="utf-8")
    assert any(not ok and "note.md" in m for ok, m in _run(tmp_path, payload))


def test_real_registry_passes() -> None:
    assert [message for ok, message in MOD.run() if not ok] == []


def test_strict_entry_requires_baseline(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("worth 1x\n", encoding="utf-8")
    payload = {
        "entries": [{"id": "s", "kind": "strict", "display": "1x", "must_appear_in": ["README.md"]}]
    }
    assert any(not ok and "must name its baseline" in m for ok, m in _run(tmp_path, payload))
    payload["entries"][0]["baseline"] = "vs the strongest naive arm"
    assert not [m for ok, m in _run(tmp_path, payload) if not ok]


def test_display_only_entry_needs_no_baseline(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("worth 1x\n", encoding="utf-8")
    payload = {
        "entries": [
            {"id": "d", "kind": "display-only", "display": "1x", "must_appear_in": ["README.md"]}
        ]
    }
    assert not [m for ok, m in _run(tmp_path, payload) if not ok]


def test_symbol_exists_checks_module_and_attribute() -> None:
    assert MOD._symbol_exists("pathlib:Path")
    assert not MOD._symbol_exists("pathlib:DefinitelyMissing")
    assert not MOD._symbol_exists("not_a_module:thing")
    assert MOD._symbol_exists("pathlib")


def test_capability_guard_passes_when_symbol_and_test_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "doc.md").write_text("This project uses lambda-MMR packing.\n", encoding="utf-8")
    monkeypatch.setattr(
        MOD,
        "CAPABILITY_REGISTRY",
        [
            {
                "phrase": "lambda-MMR",
                "symbol": "kernel.router.packer:_mmr_order",
                "test": "tests.test_packer:test_mmr_lowers_redundancy_at_equal_utility",
            }
        ],
    )
    report: list[tuple[bool, str]] = []
    MOD._check_capabilities(tmp_path, report)
    assert all(ok for ok, _ in report)
    assert any("lambda-MMR" in message for _, message in report)


def test_capability_guard_fails_when_symbol_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "doc.md").write_text("We now support orbital mind control.\n", encoding="utf-8")
    monkeypatch.setattr(
        MOD,
        "CAPABILITY_REGISTRY",
        [{"phrase": "orbital mind control", "symbol": "no.such.module:Thing"}],
    )
    report: list[tuple[bool, str]] = []
    MOD._check_capabilities(tmp_path, report)
    failures = [message for ok, message in report if not ok]
    assert failures
    assert "orbital mind control" in failures[0]
    assert "symbol no.such.module:Thing" in failures[0]
