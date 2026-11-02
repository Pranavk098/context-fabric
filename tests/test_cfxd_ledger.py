"""cfxd ledger tests: exact numbers on hand-built transcripts, receipts and diet records."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cfxd import __version__
from cfxd.ledger import format_ledger, ledger
from cfxd.refetch import refetch
from cfxd.replay import replay

ROOT = Path(__file__).resolve().parents[1]
_MODEL = "claude-sonnet-4-5-20250929"
_CACHE_READ = 0.3  # sonnet-4-5 cache-read price per Mtok
_NOW = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)


def _usage(input_tokens: int = 1000, output_tokens: int = 10, cr: int = 0) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cr,
    }


def _assistant(usage: dict, ts: datetime) -> dict:
    return {
        "type": "assistant",
        "timestamp": ts.isoformat(),
        "message": {"model": _MODEL, "content": [], "usage": usage},
    }


def _session(root: Path, project: str, session_id: str, start: datetime, lines: list[dict]) -> Path:
    path = root / project / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    os.utime(path, (start.timestamp(),) * 2)
    return path


def _write_log(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def _apply_record(home: Path, created_at: str, undone_at: str | None = None) -> Path:
    path = home / "diet" / "applied-20260110T090000000000Z.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "created_at": created_at,
        "updated_at": created_at,
        "undone_at": undone_at,
        "plugins": [
            {
                "id": "alpha@marketplace",
                "scope": "user",
                "enabled_before": True,
                "enabled_after": False,
                "applied": True,
                "est_tokens_per_turn": 600.0,
                "disabled_at": created_at,
            }
        ],
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _diet_fixture(tmp_path: Path, after_count: int) -> Path:
    root = tmp_path / "root"
    for index in range(3):
        start = datetime(2026, 1, 5, 10, index, tzinfo=UTC)
        _session(root, "proj", f"before{index}", start, [_assistant(_usage(10000), start)])
    apply_at = "2026-01-10T09:00:00Z"
    for index in range(after_count):
        start = datetime(2026, 1, 11, 10, index, tzinfo=UTC)
        _session(root, "proj", f"after{index}", start, [_assistant(_usage(4000), start)])
    _apply_record(tmp_path / "records", apply_at)
    return root


def test_diet_before_after_medians(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CFX_HOME", str(tmp_path / "records"))
    root = _diet_fixture(tmp_path, after_count=3)

    report = ledger(root, 30, now=_NOW)

    entry = report["diet"]["applies"][0]
    assert entry["plugins"] == 1
    assert (entry["before_n"], entry["after_n"]) == (3, 3)
    assert entry["before_median"] == 10000.0
    assert entry["after_median"] == 4000.0
    assert entry["delta"] == -6000.0
    text = format_ledger(report)
    assert "10,000 tokens before (n=3)" in text
    assert "4,000 tokens after (n=3), delta -6,000 tokens" in text
    assert "(OBSERVED)" in text
    assert "ESTIMATED ~600 tokens/turn removed at apply" in text
    assert "Other changes also move this number; it is not a counterfactual." in text


def test_diet_needs_three_sessions_after(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CFX_HOME", str(tmp_path / "records"))
    root = _diet_fixture(tmp_path, after_count=2)

    report = ledger(root, 30, now=_NOW)

    entry = report["diet"]["applies"][0]
    assert entry["after_n"] == 2
    assert entry["delta"] is None
    assert "not enough sessions after the apply yet (n=2)" in format_ledger(report)


def test_undone_diet_record_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CFX_HOME", str(tmp_path / "records"))
    root = _diet_fixture(tmp_path, after_count=3)
    _apply_record(tmp_path / "records", "2026-01-10T09:00:00Z", undone_at="2026-01-12T00:00:00Z")

    report = ledger(root, 30, now=_NOW)

    assert report["diet"]["applies"] == []
    assert "n/a (no applied diet records)" in format_ledger(report)


def test_coach_idle_counts_from_receipts(tmp_path: Path) -> None:
    root = tmp_path / "root"
    start = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
    _session(root, "proj", "s1", start, [_assistant(_usage(), start)])
    state = tmp_path / "state"
    ts = "2026-01-15T10:05:00+00:00"
    _write_log(
        state / "coach.jsonl",
        [{"ts": ts, "session_id": "s", "event": "coach_nudge", "outcome": "shown"}],
    )
    _write_log(
        state / "resume.jsonl",
        [
            {"ts": ts, "session_id": "s", "event": "resume", "context_tokens": 1000},
            {
                "ts": ts,
                "session_id": "s",
                "event": "resume_cold",
                "context_tokens": 1000,
                "est_write_usd": 0.5,
            },
        ],
    )

    report = ledger(root, 30, state, now=_NOW)

    notices = report["notices"]
    assert notices == {
        "coach_nudges": 1,
        "idle_notices": 2,
        "cold_resumes": 1,
    }
    assert "coach nudges 1, idle notices 2 (cold resumes 1)" in format_ledger(report)


def test_ceiling_equals_the_replay_model(tmp_path: Path) -> None:
    root = tmp_path / "root"
    start = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
    lines = [
        _assistant(_usage(20000), start),
        {"type": "user", "isCompactSummary": True},
    ]
    for index in range(1, 45):
        lines.append(_assistant(_usage(5000, 100, 1000), start + timedelta(minutes=index)))
    _session(root, "proj", "long", start, lines)
    since = "30d"
    f_median = refetch(root, since, now=_NOW)["f"]["median"]
    assert f_median is not None
    model = replay(
        root,
        since,
        ks=(),
        batches=(),
        reset_ks=(40,),
        summary_tokens=(2000,),
        refetch=(f_median,),
        now=_NOW,
    )
    expected = next(
        v["pct_saved"]
        for p in model["reset_policies"]
        for v in p["variants"]
        if v["variant"] == "upper_bound" and v["f"] == f_median
    )

    report = ledger(root, 30, now=_NOW)

    assert report["ceiling"]["pct_bounded"] == expected
    assert report["ceiling"]["f"] == f_median
    assert f"bounds {expected:.1f}% of your API-equivalent dollars" in format_ledger(report)
    assert "offline ceiling, assumes a perfect handoff; not a forecast" in format_ledger(report)


def test_header_and_proven_lines(tmp_path: Path) -> None:
    root = tmp_path / "root"
    start = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
    _session(root, "proj", "s1", start, [_assistant(_usage(), start)])

    text = format_ledger(ledger(root, 30, now=_NOW))

    assert (
        "No saving is proven. Lines are labelled OBSERVED, ESTIMATED or PROVEN; "
        "PROVEN needs the pre-registered paired test (evals/plugin_ab/PROTOCOL_V2.md)."
    ) in text
    assert "PROVEN: none yet" in text


def test_empty_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CFX_HOME", str(tmp_path / "records"))
    empty = tmp_path / "empty"
    empty.mkdir()

    report = ledger(empty, 30, now=_NOW)
    text = format_ledger(report)

    assert report["sessions"] == 0
    assert report["diet"]["applies"] == []
    assert report["notices"] is None
    assert report["ceiling"]["pct_bounded"] is None
    assert "No transcripts found in this window." in text
    assert "PROVEN: none yet" in text


def test_json_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CFX_HOME", str(tmp_path / "records"))
    root = _diet_fixture(tmp_path, after_count=3)

    report = ledger(root, 30, now=_NOW, share=True)
    data = json.loads(json.dumps(report))

    assert data["version"] == __version__
    assert data["days"] == 30
    assert data["proven"] == "none yet"
    assert data["share"] is True
    assert data["safe_to_paste"] == "Safe to paste: counts, sizes and model ids only."
    assert set(data) >= {
        "version",
        "days",
        "sessions",
        "header",
        "diet",
        "notices",
        "ceiling",
        "proven",
        "share",
    }
    assert set(data["ceiling"]) == {"label", "k", "summary_tokens", "f", "pct_bounded"}
    assert set(data["diet"]["applies"][0]) >= {
        "at",
        "plugins",
        "before_n",
        "after_n",
        "before_median",
        "after_median",
        "delta",
        "status",
    }


def test_share_canary_strings_never_leak(tmp_path: Path) -> None:
    canaries = {
        "project": "CANARYPROJ-1111",
        "prompt": "CANARYPROMPT-2222",
        "skill": "CANARYSKILL-3333",
    }
    start = datetime.now(UTC)
    lines = [
        _assistant(_usage(10000), start),
        {
            "type": "user",
            "message": {"content": [{"type": "text", "text": canaries["prompt"]}]},
        },
        {
            "type": "attachment",
            "attachment": {
                "type": "skill_listing",
                "names": [canaries["skill"]],
                "content": f"- {canaries['skill']}: does things",
            },
        },
    ]
    _session(tmp_path / "root", canaries["project"], "s1", start, lines)
    env = {**os.environ, "CFX_HOME": str(tmp_path / "state")}

    for extra in (["--share"], ["--share", "--json"]):
        result = subprocess.run(
            [sys.executable, "-m", "cfxd", "ledger", "--root", str(tmp_path / "root"), *extra],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        for canary in canaries.values():
            assert canary not in result.stdout
            assert canary not in result.stderr
        if "--json" not in extra:
            assert "Safe to paste: counts, sizes and model ids only." in result.stdout
            assert f"cfxd ledger {__version__}" in result.stdout
