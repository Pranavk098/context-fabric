"""Bound and coach replay script on synthetic transcripts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bound_coach_replay as r  # noqa: E402

_USAGE = {
    "input_tokens": 100,
    "output_tokens": 100,
    "cache_read_input_tokens": 160_000,
}


def _use(mid: str, uid: str, command: str, **extra: Any) -> dict[str, Any]:
    msg = {
        "id": mid,
        "model": "claude-sonnet-4-5",
        "usage": _USAGE,
        "stop_reason": "tool_use",
        "content": [
            {
                "type": "tool_use",
                "id": uid,
                "name": "Bash",
                "input": {"command": command, **extra},
            }
        ],
    }
    return {"type": "assistant", "message": msg}


def _result(uid: str, text: str) -> dict[str, Any]:
    block = {"type": "tool_result", "tool_use_id": uid, "content": text}
    return {"type": "user", "message": {"content": [block]}}


def _turn(mid: str, context: int = 160_000) -> dict[str, Any]:
    msg = {
        "id": mid,
        "model": "claude-sonnet-4-5",
        "usage": {**_USAGE, "cache_read_input_tokens": context},
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "ok"}],
    }
    return {"type": "assistant", "message": msg}


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _long_output() -> str:
    lines = ["Traceback (most recent call last):\n"]
    lines += [f"line {i}\n" for i in range(1, 200)]
    return "".join(lines)


def test_bound_replays_shipped_rule_and_flags_dropped_error() -> None:
    rows = [_use("m1", "b1", "pytest tests/"), _result("b1", _long_output())]
    scanned = r.scan_bound(rows)
    records = scanned["records"]
    assert len(records) == 1
    assert records[0]["rule"] == "shipped"
    assert records[0]["key"] == "pytest"
    assert records[0]["dropped"] is True
    assert records[0]["dropped_error"] is True
    assert records[0]["error_lost"] is True
    assert scanned["stats"]["bash_results"] == 1

    summary = r.summarise_bound(records)
    assert summary["n"] == 1
    assert summary["n_dropped"] == 1
    assert summary["n_dropped_error"] == 1
    assert summary["dropped_error_share"] == 1.0
    assert summary["saved_tokens"] > 0
    assert summary["saved_tokens"] < summary["full_tokens"]


def test_bound_separates_compound_and_unrecognised_commands() -> None:
    rows = [
        _use("m1", "b1", "cd x && uv run pytest -q tests"),
        _result("b1", _long_output()),
        _use("m2", "b2", "ls -la"),
        _result("b2", "a\nb\n"),
        _use("m3", "b3", "pytest tests/", run_in_background=True),
        _result("b3", _long_output()),
    ]
    scanned = r.scan_bound(rows)
    assert [x["rule"] for x in scanned["records"]] == ["recognised"]
    assert scanned["stats"]["not_recognised"] == 1
    assert scanned["stats"]["background"] == 1
    assert r.summarise_bound(scanned["records"])["n"] == 0
    assert r.summarise_bound(scanned["records"], "recognised")["n"] == 1


def test_bound_short_output_is_not_dropped() -> None:
    rows = [_use("m1", "b1", "git log --oneline"), _result("b1", "a\nb\nc\n")]
    summary = r.summarise_bound(r.scan_bound(rows)["records"])
    assert summary["n"] == 1
    assert summary["n_dropped"] == 0
    assert summary["saved_tokens"] == 0
    assert summary["saved_share"] == 0.0


def test_coach_nudge_at_turn_threshold_and_cooldown() -> None:
    assert r.coach_nudges([(160_000, None)] * 29) == []
    assert r.coach_nudges([(160_000, None)] * 40) == [30]
    # Ten turns later, growth is reset by the cooldown only if context grew enough.
    assert r.coach_nudges([(160_000, None)] * 39 + [(260_000, None)]) == [30, 40]
    assert r.coach_nudges([(100_000, None)] * 40) == []


def test_scan_coach_counts_turns_and_prices_session() -> None:
    rows = [_turn(f"m{i}") for i in range(35)]
    coach = r.scan_coach(rows, r._load_prices(r._DEFAULT_PRICES))
    assert len(coach["turns"]) == 35
    assert coach["cost"] > 0
    assert coach["unpriced"] == 0
    assert r.coach_nudges(coach["turns"]) == [30]


def test_coach_precision_and_recall_against_top_decile() -> None:
    high = [(160_000, None)] * 30
    sessions = [
        {"session": "a", "cost": 100.0, "unpriced": 0, "turns": high},
        {"session": "b", "cost": 1.0, "unpriced": 0, "turns": high},
        {"session": "c", "cost": 0.5, "unpriced": 0, "turns": high},
    ]
    summary = r.summarise_coach(sessions)
    assert summary["top_k"] == 1
    assert summary["n_fired"] == 3
    assert summary["n_fired_top"] == 1
    assert summary["precision"] == 1 / 3
    assert summary["recall"] == 1.0
    assert summary["median_first_nudge"] == 30

    quiet = [{"session": "a", "cost": 100.0, "unpriced": 0, "turns": [(160_000, None)] * 5}]
    quiet.append({"session": "b", "cost": 1.0, "unpriced": 0, "turns": high})
    summary = r.summarise_coach(quiet)
    assert summary["precision"] == 0.0
    assert summary["recall"] == 0.0


def test_collect_and_render_are_aggregates_only(tmp_path: Path) -> None:
    canary = "CANARY-9f8e7d6c5b4a3210-NEVER-STORE"
    rows = [
        _use("m1", "b1", f"pytest --canary {canary}"),
        _result("b1", _long_output()),
    ]
    rows += [_turn(f"c{i}") for i in range(30)]
    _write(tmp_path / "proj" / "sess.jsonl", rows)
    _write(tmp_path / "copy" / "sess.jsonl", rows)

    data = r.collect(tmp_path, r._load_prices(r._DEFAULT_PRICES))
    assert len(data["sessions"]) == 1
    assert data["stats"]["duplicate_session_copies_skipped"] == 1
    text = r.render(data)
    assert canary not in text
    assert str(tmp_path) not in text
    assert "Precision" in text
    assert "Shipped rule" in text
