"""cfxd coverage tests: hand-built transcripts and events with exact delivery rates."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from cfxd.audit import coverage, format_coverage
from cfxd.cli import _hook_delivery_warning
from cfxd.week import format_week, week

ROOT = Path(__file__).resolve().parents[1]


def _tool_use(tool_id: str) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": "Read", "input": {"file_path": "a.txt"}}


def _session(root: Path, project: str, session_id: str, tool_calls: int) -> Path:
    path = root / project / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = [_tool_use(f"{session_id}-t{i}") for i in range(tool_calls)]
    line = {"type": "assistant", "message": {"content": blocks, "usage": {}}}
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    return path


def _events(state: Path, entries: list[dict]) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )


def _post(session_id: str | None, count: int, now: datetime | None = None) -> list[dict]:
    ts = (now or datetime.now(UTC)).isoformat()
    return [
        {"ts": ts, "event": "PostToolUse", "session_id": session_id, "tool_name": "Read"}
        for _ in range(count)
    ]


def test_exact_rates_and_sessions_without_events(tmp_path: Path) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    for sid, calls in (("a", 4), ("b", 4), ("c", 4), ("d", 4)):
        _session(root, "proj", sid, calls)
    _events(state, [*_post("a", 4), *_post("b", 2), *_post("c", 1)])

    report = coverage(root, state, days=7)

    assert report["sessions_compared"] == 3
    assert report["sessions_without_events"] == 1
    assert report["tool_calls"] == 12
    assert report["events_received"] == 7
    assert report["delivery"] == {
        "median": 0.5,
        "p25": 0.375,
        "p75": 0.75,
        "overall": round(7 / 12, 4),
    }
    assert report["below_half_share"] == round(1 / 3, 4)
    assert report["na_reason"] is None


def test_no_events_at_all_is_na(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _session(root, "proj", "a", 3)

    report = coverage(root, tmp_path / "state", days=7)

    assert report["sessions_compared"] == 0
    assert report["sessions_without_events"] == 1
    assert report["delivery"] is None
    assert report["below_half_share"] is None
    assert "n/a" in format_coverage(report)


def test_missing_session_id_is_na_not_guessed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    _session(root, "proj", "a", 4)
    _events(state, [*_post("a", 4), *_post(None, 1)])

    report = coverage(root, state, days=7)

    assert report["delivery"] is None
    assert report["below_half_share"] is None
    assert report["unattributed_events"] == 1
    assert "session id" in report["na_reason"]


def test_json_shape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    _session(root, "proj", "a", 2)
    _events(state, _post("a", 2))

    data = json.loads(json.dumps(coverage(root, state, days=7)))

    assert set(data) == {
        "days",
        "sessions_compared",
        "sessions_without_events",
        "tool_calls",
        "events_received",
        "delivery",
        "below_half_share",
        "unattributed_events",
        "na_reason",
    }
    assert set(data["delivery"]) == {"median", "p25", "p75", "overall"}


def test_doctor_warning_threshold_edge(tmp_path: Path) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    _session(root, "proj", "a", 100)
    _events(state, _post("a", 69))
    below = coverage(root, state, days=7)
    assert below["delivery"] is not None
    assert below["delivery"]["overall"] == 0.69
    warning = _hook_delivery_warning(below)
    assert warning is not None
    assert "checkpoints are completed from the transcript" in warning

    _events(state, _post("a", 70))
    at = coverage(root, state, days=7)
    assert at["delivery"] is not None
    assert at["delivery"]["overall"] == 0.70
    assert _hook_delivery_warning(at) is None


def test_week_hook_delivery_line(tmp_path: Path) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    for sid in ("a", "b"):
        _session(root, "proj", sid, 5)
    _events(state, [*_post("a", 3), *_post("b", 3)])

    text = format_week(week(root, 7, state_dir=state))

    assert "Hook delivery: 60% of tool calls observed (2 sessions)" in text

    no_state = format_week(week(root, 7))
    assert "Hook delivery: n/a (no state directory)" in no_state


def test_cli_canary_strings_never_leak(tmp_path: Path) -> None:
    root = tmp_path / "CANARYPROJ-root"
    state = tmp_path / "state"
    sid = "CANARYSID-1111"
    path_canary = "CANARYPATH-2222"
    path = root / "proj" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Read",
                    "input": {"file_path": f"C:\\u\\{path_canary}\\secret.txt"},
                }
            ],
            "usage": {},
        },
    }
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    _events(state, _post(sid, 1))

    for extra in (["--json"], []):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "cfxd",
                "coverage",
                "--root",
                str(root),
                "--state-dir",
                str(state),
                *extra,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        for canary in (sid, path_canary, "CANARYPROJ-root"):
            assert canary not in result.stdout
            assert canary not in result.stderr


def test_cli_rejects_bad_days(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "coverage", "--root", str(tmp_path), "--days", "0"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2
    assert "--days must be at least 1" in result.stderr
