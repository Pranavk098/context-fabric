"""``cfxd brief``: receipts view over briefs.jsonl."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from cfxd.brief_receipts import join_receipts, path_sha16, receipts_to_json
from cfxd.cli import main

FIXTURES = Path(__file__).parent / "fixtures" / "real_subagent"
SID = "11111111-2222-4333-8444-555555555555"
TID = "toolu_01A2VzHZ4ZGjA5Eg3oriZUUq"


def _setup(tmp_path: Path, extra: list[dict[str, Any]] | None = None) -> tuple[Path, Path, Path]:
    root = tmp_path / "projects"
    shutil.copytree(FIXTURES, root / "p")
    state = tmp_path / "state"
    state.mkdir()
    first = {
        "session_id": SID,
        "tool_use_id": TID,
        "decision": "briefed",
        "packet_bytes": 0,
        "counts": {},
        "budget_bytes": 6000,
        "target_model": "haiku",
    }
    log = state / "briefs.jsonl"
    log.write_text("".join(json.dumps(e) + "\n" for e in [first, *(extra or [])]), encoding="utf-8")
    return root, state, log


def test_json_matches_receipts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, state, log = _setup(tmp_path)
    assert main(["brief", "--json", "--root", str(root), "--state-dir", str(state)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["receipts"] == receipts_to_json(join_receipts(log, root))
    r = out["receipts"][0]
    assert r["matched"] and r["child_tokens"]["total"] == 130036


def test_text_shows_r1_numbers(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, state, _ = _setup(tmp_path)
    assert main(["brief", "--root", str(root), "--state-dir", str(state)]) == 0
    text = capsys.readouterr().out
    assert "130,036" in text and "claude-haiku-4-5-20251001" in text
    assert "completed" in text and "0 of 0 briefed files" in text
    assert "of 6,000 budget" in text


def test_items_and_reasons_use_recovered_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, state, log = _setup(tmp_path)
    (r,) = join_receipts(log, root)
    entry = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    entry["items"] = [
        {"p": path_sha16(r.child_files_read[0]), "r": ["prompt_match", "edited"], "x": []},
        {"p": "0" * 16, "r": ["recent"], "x": []},
    ]
    log.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    assert main(["brief", "--root", str(root), "--state-dir", str(state)]) == 0
    text = capsys.readouterr().out
    assert "[prompt_match, edited]" in text
    assert "(path not recoverable)  [recent]" in text


def test_unmatched_brief_reports_reason(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, state, _ = _setup(
        tmp_path,
        [{"session_id": "ghost", "tool_use_id": "t9", "decision": "skipped", "skip_reason": "x"}],
    )
    assert main(["brief", "--root", str(root), "--state-dir", str(state)]) == 0
    text = capsys.readouterr().out
    assert "unmatched" in text and "no_parent_transcript" in text


def test_session_filter(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, state, _ = _setup(
        tmp_path, [{"session_id": "ghost", "tool_use_id": "t9", "decision": "briefed"}]
    )
    args = ["brief", "--json", "--root", str(root), "--state-dir", str(state)]
    assert main([*args, "--session", SID]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["session_id"] for r in out["receipts"]] == [SID]
    assert main([*args, "--session", "nope"]) == 0
    assert json.loads(capsys.readouterr().out)["receipts"] == []
    assert main(["brief", "--session", "nope", "--state-dir", str(state)]) == 0
    assert "No briefs logged for session nope" in capsys.readouterr().out


def test_empty_state_explains_how_to_turn_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["brief", "--state-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "No briefs have been logged yet" in text
    assert "Brief v1 is off" in text and "cfxd set brief_v1 on" in text


def test_empty_state_does_not_tell_you_to_turn_on_when_it_is_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "config.json").write_text('{"brief_v1": true}', encoding="utf-8")
    assert main(["brief", "--state-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "Brief v1 is on" in text
    assert "Turn it on" not in text and "cfxd set brief_v1 on" not in text
