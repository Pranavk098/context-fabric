"""cfxd stats tests: fold/hook aggregation from the daemon logs."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cfxd.stats import collect_stats, format_human

ROOT = Path(__file__).resolve().parents[1]


def _write_logs(state_dir: Path, folds: list[str], events: list[str]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    if folds:
        (state_dir / "folds.jsonl").write_text("\n".join(folds) + "\n", encoding="utf-8")
    if events:
        (state_dir / "events.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")


def _fold_entry(**overrides: object) -> str:
    entry: dict[str, object] = {
        "ts": datetime.now(UTC).isoformat(),
        "session_id": "s1",
        "tool_name": "Read",
        "field": "file.content",
        "kind": "code",
        "bytes_in": 1000,
        "bytes_out": 100,
        "obs_id": "abc123",
    }
    entry.update(overrides)
    return json.dumps(entry)


def _event_entry(**overrides: object) -> str:
    entry: dict[str, object] = {
        "ts": datetime.now(UTC).isoformat(),
        "event": "PreToolUse",
        "tool_name": "Read",
        "session_id": "s1",
        "latency_ms": 0.2,
        "outcome": "handled",
        "bytes_in": 100,
        "bytes_out": 0,
    }
    entry.update(overrides)
    return json.dumps(entry)


def test_collect_exact_counts(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    folds = [
        _fold_entry(tool_name="Read", bytes_in=1000, bytes_out=100),
        _fold_entry(tool_name="Read", bytes_in=2000, bytes_out=200),
        _fold_entry(tool_name="Bash", session_id="s2", bytes_in=3000, bytes_out=300),
        _fold_entry(tool_name="Grep", bytes_in=4000, bytes_out=400),
    ]
    _write_logs(state_dir, folds, [])
    s = collect_stats(state_dir)
    assert s["folds"]["count"] == 4
    assert s["folds"]["by_tool"] == {"Read": 2, "Bash": 1, "Grep": 1}
    assert s["output"]["bytes_in"] == 10000
    assert s["output"]["bytes_out"] == 1000
    assert s["output"]["bytes_saved"] == 9000
    assert s["output"]["percent_saved"] == 90
    assert s["output"]["tokens_estimated"] == 2250
    assert s["sessions"] == 2


def test_collect_hooks(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    events = [
        _event_entry(latency_ms=0.2),
        _event_entry(latency_ms=1.1),
        _event_entry(latency_ms=0.5, outcome="error"),
        _event_entry(latency_ms=0.3, outcome="rejected", session_id=None),
    ]
    _write_logs(state_dir, [], events)
    s = collect_stats(state_dir)
    assert s["hooks"]["count"] == 4
    assert s["hooks"]["errors"] == 1
    assert s["hooks"]["rejected"] == 1
    assert 0.0 <= s["hooks"]["p50_ms"] <= s["hooks"]["p95_ms"] <= 5.0


def test_since_filter(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    now = datetime.now(UTC)
    folds = [
        _fold_entry(ts=(now - timedelta(hours=1)).isoformat()),
        _fold_entry(ts=(now - timedelta(days=2)).isoformat(), tool_name="Bash"),
        _fold_entry(ts=(now - timedelta(days=10)).isoformat(), tool_name="Grep"),
    ]
    _write_logs(state_dir, folds, [])
    assert collect_stats(state_dir, "all", now=now)["folds"]["count"] == 3
    assert collect_stats(state_dir, "7d", now=now)["folds"]["count"] == 2
    assert collect_stats(state_dir, "24h", now=now)["folds"]["count"] == 1


def test_session_filter(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    folds = [
        _fold_entry(session_id="s1", bytes_in=1000, bytes_out=100),
        _fold_entry(session_id="s2", tool_name="Bash", bytes_in=2000, bytes_out=200),
    ]
    events = [
        _event_entry(session_id="s1"),
        _event_entry(session_id="s2", outcome="error"),
    ]
    _write_logs(state_dir, folds, events)
    s = collect_stats(state_dir, "all", "s1")
    assert s["folds"]["count"] == 1
    assert s["folds"]["by_tool"] == {"Read": 1}
    assert s["hooks"]["count"] == 1
    assert s["hooks"]["errors"] == 0


def test_malformed_lines_skipped(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    now = datetime.now(UTC).isoformat()
    lines = [
        "this is not json",
        json.dumps(["not", "a", "dict"]),
        json.dumps({"ts": now, "tool_name": "Read", "bytes_in": "nope", "bytes_out": 100}),
        _fold_entry(bytes_in=1000, bytes_out=100),
        _fold_entry(tool_name="Bash", bytes_in=500, bytes_out=50),
        "",
    ]
    _write_logs(state_dir, lines, [])
    s = collect_stats(state_dir)
    assert s["folds"]["count"] == 2
    assert s["output"]["bytes_in"] == 1500


def test_empty_and_missing_logs(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    _write_logs(state_dir, [], [])
    s = collect_stats(state_dir)
    assert s["folds"]["count"] == 0
    assert s["hooks"]["count"] == 0

    missing = collect_stats(tmp_path / "does-not-exist")
    assert missing["folds"]["count"] == 0
    assert missing["hooks"]["count"] == 0
    assert format_human(missing) == "No folds yet."


def test_json_output_parses(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    _write_logs(state_dir, [_fold_entry(bytes_in=1000, bytes_out=100)], [])
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "stats", "--state-dir", str(state_dir), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["folds"]["count"] == 1
    assert data["output"]["bytes_saved"] == 900
    assert data["output"]["tokens_estimated"] == 225


def test_cli_human_output(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    _write_logs(state_dir, [_fold_entry(bytes_in=1000, bytes_out=100)], [])
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "stats", "--state-dir", str(state_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Folds:" in result.stdout
    assert "Read 1" in result.stdout
    assert "1,000 bytes -> 100 bytes" in result.stdout
    assert "did not have to read" in result.stdout
    assert "about 225 tokens" in result.stdout


def test_no_folds_subprocess_exits_0(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    state_dir.mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "stats", "--state-dir", str(state_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "No folds yet."


def test_output_never_contains_content(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    marker = "SEKRIT-MARKER-abcdef123456"
    now = datetime.now(UTC).isoformat()
    folds = [
        json.dumps(
            {
                "ts": now,
                "session_id": "s1",
                "tool_name": "Read",
                "field": "file.content",
                "kind": "code",
                "bytes_in": 50000,
                "bytes_out": 500,
                "obs_id": "0123456789abcdef",
                "content": marker,
            }
        )
    ]
    events = [
        json.dumps(
            {
                "ts": now,
                "event": "PostToolUse",
                "tool_name": "Read",
                "session_id": "s1",
                "latency_ms": 0.5,
                "outcome": "handled",
                "bytes_in": 50000,
                "bytes_out": 500,
                "payload": marker,
            }
        )
    ]
    _write_logs(state_dir, folds, events)
    for extra in ([], ["--json"]):
        result = subprocess.run(
            [sys.executable, "-m", "cfxd", "stats", "--state-dir", str(state_dir), *extra],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert marker not in result.stdout
        assert marker not in result.stderr
