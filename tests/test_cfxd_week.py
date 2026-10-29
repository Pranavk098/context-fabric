"""cfxd week tests: hand-built transcripts with exact bucket and share numbers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cfxd import __version__
from cfxd.week import week

ROOT = Path(__file__).resolve().parents[1]
_MODEL = "claude-sonnet-4-5-20250929"
# (1000 input * 3 + 100 output * 15) / 1e6 dollars per turn.
_TURN_DOLLARS = 0.0045


def _usage(input_tokens: int = 1000, output_tokens: int = 100, cr: int = 0) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cr,
    }


def _assistant(usage: dict, *blocks: dict) -> dict:
    return {
        "type": "assistant",
        "message": {"model": _MODEL, "content": list(blocks), "usage": usage},
    }


def _session(root: Path, project: str, session_id: str, lines: list[dict]) -> Path:
    path = root / project / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _turns(n: int, usage: dict | None = None) -> list[dict]:
    return [_assistant(usage or _usage()) for _ in range(n)]


def _write_log(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def test_bucket_counts_dollars_and_shares(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "short", _turns(2))
    _session(tmp_path, "proj", "medium", _turns(11))
    _session(tmp_path, "proj", "long", _turns(31))

    report = week(tmp_path, 7)

    assert report["sessions"] == 3
    assert report["sessions_under_2_turns"] == 0
    buckets = report["buckets"]
    assert set(buckets) == {"2-10", "11-30", "31+"}
    assert [buckets[k]["sessions"] for k in buckets] == [1, 1, 1]

    assert buckets["2-10"]["dollars"] == pytest.approx(2 * _TURN_DOLLARS, abs=1e-6)
    assert buckets["11-30"]["dollars"] == pytest.approx(11 * _TURN_DOLLARS, abs=1e-6)
    assert buckets["31+"]["dollars"] == pytest.approx(31 * _TURN_DOLLARS, abs=1e-6)
    assert report["dollars_total"] == pytest.approx(44 * _TURN_DOLLARS, abs=1e-6)

    total = 44 * _TURN_DOLLARS
    assert buckets["2-10"]["share"] == pytest.approx(2 * _TURN_DOLLARS / total, abs=1e-4)
    assert buckets["11-30"]["share"] == pytest.approx(11 * _TURN_DOLLARS / total, abs=1e-4)
    assert buckets["31+"]["share"] == pytest.approx(31 * _TURN_DOLLARS / total, abs=1e-4)
    assert sum(b["share"] for b in buckets.values()) == pytest.approx(1.0, abs=1e-4)

    # 31 turns carry 70% of the dollars, so the long-session rule must fire verbatim.
    assert (
        "Most of your cost is in long sessions; the checkpoint workflow targets exactly those."
        in report["meaning"]
    )
    assert len(report["meaning"]) <= 4


def test_cache_read_share_and_first_turn_context(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", _turns(2, _usage(input_tokens=1000, output_tokens=0, cr=9000)))

    report = week(tmp_path, 7)

    assert report["cache_read_share_tokens"] == pytest.approx(0.9, abs=1e-4)
    assert report["first_turn_context_median"] == 10000
    assert report["compactions"] == 0
    assert any("Cache reads" in sentence for sentence in report["meaning"])


def test_days_filter_by_mtime(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    new = _session(tmp_path, "proj", "new", _turns(2))
    old = _session(tmp_path, "proj", "old", _turns(2))
    os.utime(new, ((now - timedelta(days=2)).timestamp(),) * 2)
    os.utime(old, ((now - timedelta(days=40)).timestamp(),) * 2)

    assert week(tmp_path, 7, now=now)["sessions"] == 1
    assert week(tmp_path, 30, now=now)["sessions"] == 1
    assert week(tmp_path, 60, now=now)["sessions"] == 2


def test_empty_root(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    report = week(empty, 7)

    assert report["sessions"] == 0
    assert report["dollars_total"] == 0.0
    assert all(b["sessions"] == 0 for b in report["buckets"].values())
    assert report["meaning"] == ["No sessions in this window yet, so there is nothing to optimise."]


def test_unavailable_numbers_are_na_not_guessed(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", [{"type": "user", "message": {"content": []}}])

    report = week(tmp_path, 7)

    assert report["sessions"] == 1
    assert report["sessions_under_2_turns"] == 1
    assert report["cache_read_share_tokens"] is None
    assert report["first_turn_context_median"] is None
    assert report["refetch"]["median_f"] is None
    assert report["diet"]["unused_skills"] is None
    assert report["receipts"]["folds"] is None
    assert report["receipts"]["checkpoints"] is None
    assert report["notes"]["first_turn"] == "no first-turn usage"
    assert report["notes"]["refetch"] == "no compaction events"
    assert report["notes"]["diet"] == "no skill listing in transcripts"
    assert report["notes"]["receipts"] == "no state directory"
    assert report["notes"]["checkpoints"] == "not recorded by cfxd stats"


def test_diet_counts_only(tmp_path: Path) -> None:
    skill = "CANARYSKILL-1234"
    content = f"- {skill}: does things"
    lines = _turns(2) + [
        {
            "type": "attachment",
            "attachment": {"type": "skill_listing", "names": [skill], "content": content},
        }
    ]
    _session(tmp_path, "proj", "s1", lines)

    report = week(tmp_path, 7)

    assert report["diet"]["unused_skills"] == 1
    assert report["diet"]["unused_connectors"] == 0
    assert report["diet"]["tokens_per_turn"] == pytest.approx(len(content.encode()) / 4, abs=0.1)
    assert skill not in json.dumps(report)


def test_receipts_from_stats(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", _turns(2))
    state = tmp_path / "state"
    _write_log(
        state / "folds.jsonl",
        [{"session_id": "s", "bytes_in": 100, "bytes_out": 50, "tool_name": "Bash"}] * 2,
    )
    _write_log(state / "events.jsonl", [{"session_id": "s", "latency_ms": 5.0, "outcome": "error"}])
    _write_log(state / "bound.jsonl", [{"event": "bound", "session_id": "s"}])

    report = week(tmp_path, 7, state)

    receipts = report["receipts"]
    assert receipts["folds"] == 2
    assert receipts["bound_rewrites"] == 1
    assert receipts["hook_calls"] == 1
    assert receipts["hook_errors"] == 1
    assert receipts["checkpoints"] is None


def test_json_shape(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", _turns(2))

    report = week(tmp_path, 7)
    data = json.loads(json.dumps(report))

    assert data["version"] == __version__
    assert data["days"] == 7
    assert set(data) >= {
        "version",
        "days",
        "sessions",
        "buckets",
        "cache_read_share_tokens",
        "first_turn_context_median",
        "compactions",
        "clear_restarts",
        "model_switches",
        "refetch",
        "diet",
        "receipts",
        "notes",
        "meaning",
    }
    assert set(data["buckets"]["31+"]) == {"sessions", "dollars", "share"}
    assert set(data["receipts"]) == {
        "folds",
        "bound_rewrites",
        "hook_calls",
        "hook_errors",
        "hook_rejected",
        "checkpoints",
        "cold_resumes",
    }


def test_share_canary_strings_never_leak(tmp_path: Path) -> None:
    canaries = {
        "project": "CANARYPROJ-1111",
        "skill": "CANARYSKILL-2222",
        "prompt": "CANARYPROMPT-3333",
        "command": "CANARYCMD-4444",
        "path": "CANARYPATH-5555",
    }
    lines = [
        _assistant(
            _usage(),
            {"type": "text", "text": canaries["prompt"]},
            {
                "type": "tool_use",
                "id": "b1",
                "name": "Bash",
                "input": {"command": f"echo {canaries['command']}"},
            },
            {
                "type": "tool_use",
                "id": "r1",
                "name": "Read",
                "input": {"file_path": f"C:\\u\\{canaries['path']}\\secret.txt"},
            },
        ),
        {"type": "user", "message": {"content": [{"type": "text", "text": canaries["prompt"]}]}},
        {
            "type": "attachment",
            "attachment": {
                "type": "skill_listing",
                "names": [canaries["skill"]],
                "content": f"- {canaries['skill']}: does things",
            },
        },
        _assistant(_usage()),
    ]
    _session(tmp_path, canaries["project"], "s1", lines)

    for extra in (["--share"], ["--share", "--json"]):
        result = subprocess.run(
            [sys.executable, "-m", "cfxd", "week", "--root", str(tmp_path), *extra],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        for canary in canaries.values():
            assert canary not in result.stdout
            assert canary not in result.stderr
        if "--json" not in extra:
            assert "Safe to paste: counts, sizes and model ids only." in result.stdout
            assert f"cfxd week {__version__}" in result.stdout


def test_cli_rejects_bad_days(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "week", "--root", str(tmp_path), "--days", "0"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2
    assert "--days must be at least 1" in result.stderr
