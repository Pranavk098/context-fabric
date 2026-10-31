"""``cfxd checkup``: fixed section order, no MCP churn, and canaries never surface."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import cfxd.checkup as checkup

_SAFE = "safe to paste: counts and percentages only"
_MODEL = "claude-haiku-4-5-20251001"
_CHILD_MODEL = "claude-sonnet-5-5"
_PARENT_USAGE = {"input_tokens": 1000, "output_tokens": 100}
_CHILD_USAGE = {
    "input_tokens": 100,
    "output_tokens": 50,
    "cache_read_input_tokens": 1000,
    "cache_creation_input_tokens": 0,
}


def _assistant(mid: str, content: list[dict[str, Any]], usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"id": mid, "model": _MODEL, "usage": usage, "content": content},
    }


def _read(uid: str, path: Path) -> dict[str, Any]:
    block = {"type": "tool_use", "id": uid, "name": "Read", "input": {"file_path": str(path)}}
    return _assistant(uid, [block], _PARENT_USAGE)


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _seed(root: Path, state: Path) -> None:
    """Parent with a skill listing, three reads, an Agent spawn and a compaction."""
    files = [root / "proj" / "zzcanary" / f"zzcanary_{i}.py" for i in range(4)]
    skill = _assistant(
        "p0",
        [{"type": "tool_use", "id": "r0", "name": "Read", "input": {"file_path": str(files[0])}}],
        _PARENT_USAGE,
    )
    reads = _assistant(
        "p1",
        [
            {"type": "tool_use", "id": f"r{i}", "name": "Read", "input": {"file_path": str(p)}}
            for i, p in enumerate(files[:3])
        ],
        _PARENT_USAGE,
    )
    spawn = _assistant(
        "p2",
        [
            {
                "type": "tool_use",
                "id": "ag1",
                "name": "Agent",
                "input": {"prompt": "zzcanary prompt", "subagent_type": "explore"},
            }
        ],
        _PARENT_USAGE,
    )
    result = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "ag1", "content": "ok"}]},
        "toolUseResult": {"agentId": "kid1", "status": "completed"},
    }
    summary = {
        "type": "user",
        "isCompactSummary": True,
        "compactMetadata": {"trigger": "auto"},
        "message": {"content": f"Summary: kept {files[0]} and {files[1]}; nothing else matters."},
    }
    _write(
        root / "proj" / "sess1.jsonl",
        [
            {
                "type": "attachment",
                "attachment": {
                    "type": "skill_listing",
                    "names": ["zzcanary-skill"],
                    "content": "- zzcanary-skill: does things",
                },
            },
            {
                "type": "attachment",
                "attachment": {
                    "type": "deferred_tools_delta",
                    "addedNames": ["mcp__zzcanaryconn__tool"],
                },
            },
            skill,
            reads,
            spawn,
            result,
            summary,
        ],
    )
    _write(
        root / "proj" / "sess1" / "subagents" / "agent-kid1.jsonl",
        [
            {"type": "user", "message": {"content": [{"type": "text", "text": "zzcanary prompt"}]}},
            {
                "type": "assistant",
                "message": {
                    "id": "c1",
                    "model": _CHILD_MODEL,
                    "usage": _CHILD_USAGE,
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "c1",
                            "name": "Read",
                            "input": {"file_path": str(files[0])},
                        },
                        {
                            "type": "tool_use",
                            "id": "c2",
                            "name": "Read",
                            "input": {"file_path": str(files[3])},
                        },
                    ],
                },
            },
        ],
    )
    state.mkdir(parents=True, exist_ok=True)
    (state / "config.json").write_text(json.dumps({"brief_v1": True}), encoding="utf-8")


def test_sections_in_order_no_churn_and_exact_last_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "projects"
    _seed(root, tmp_path / "state")
    monkeypatch.setenv("CFX_HOME", str(tmp_path / "state"))
    assert checkup.main(["--root", str(root)]) == 0
    text = capsys.readouterr().out

    assert text.index("Diet (") < text.index("Fidelity (") < text.index("Sub-agents (")
    assert "churn" not in text.lower()
    assert "Brief on: yes" in text
    assert text.splitlines()[-1] == _SAFE


def test_canaries_never_appear_in_text_or_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "projects"
    _seed(root, tmp_path / "state")
    monkeypatch.setenv("CFX_HOME", str(tmp_path / "state"))

    assert checkup.main(["--root", str(root)]) == 0
    text = capsys.readouterr().out
    assert "zzcanary" not in text.lower()

    assert checkup.main(["--root", str(root), "--json"]) == 0
    raw = capsys.readouterr().out
    assert "zzcanary" not in raw.lower()
    assert json.loads(raw)["safe_to_paste"] == "counts and percentages only"


def test_json_parses_and_has_all_three_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "projects"
    _seed(root, tmp_path / "state")
    monkeypatch.setenv("CFX_HOME", str(tmp_path / "state"))
    assert checkup.main(["--root", str(root), "--json", "--since", "all"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["no_data"] == []
    assert report["diet"]["unused_skills"] == 1
    assert report["diet"]["unused_connectors"] == 1
    assert report["diet"]["estimate"] == "an estimate, not a bill"
    assert report["fidelity"]["M1"]["mentions"] == 2
    assert report["fidelity"]["events_analyzed"] == 1
    assert report["sub_agents"]["brief_on"] is True
    assert report["sub_agents"]["children"] == 1


def test_empty_root_gives_three_no_data_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CFX_HOME", str(tmp_path / "state"))
    assert checkup.main(["--root", str(tmp_path / "empty")]) == 0
    lines = capsys.readouterr().out.splitlines()

    assert sum(1 for line in lines if "no data" in line.lower()) == 3
    assert lines[-1] == _SAFE

    assert checkup.main(["--root", str(tmp_path / "empty"), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["no_data"] == ["diet", "fidelity", "sub_agents"]


def test_cli_entry_runs_and_exits_zero(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "checkup", "--root", str(tmp_path / "empty")],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == _SAFE
