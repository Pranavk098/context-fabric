"""``--share`` is numeric-only: canary strings never reach its output."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODEL = "claude-haiku-4-5-20251001"
CANARY = {
    "project": "CANARYPROJ-1111",
    "skill": "CANARYSKILL-2222",
    "connector": "CANARYCONN-3333",
    "path": "CANARYPATH-4444",
    "command": "CANARYCMD-5555",
    "prompt": "CANARYPROMPT-6666",
    "agent": "CANARYAGENT-7777",
    "session": "CANARYSESSION-8888",
}


def _usage(input_tokens: int = 1000, output_tokens: int = 100) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def _write_jsonl(path: Path, objects: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(o) for o in objects) + "\n", encoding="utf-8")


def _seed(root: Path, state: Path) -> None:
    """One parent transcript, one matched child, and one brief receipt."""
    sid = CANARY["session"]
    parent = root / CANARY["project"] / f"{sid}.jsonl"
    _write_jsonl(
        parent,
        [
            {
                "type": "assistant",
                "message": {
                    "model": MODEL,
                    "content": [
                        {"type": "text", "text": CANARY["prompt"]},
                        {
                            "type": "tool_use",
                            "id": "toolu_canary",
                            "name": "Agent",
                            "input": {
                                "subagent_type": CANARY["agent"],
                                "prompt": CANARY["prompt"],
                            },
                        },
                        {
                            "type": "tool_use",
                            "id": "bash1",
                            "name": "Bash",
                            "input": {"command": CANARY["command"]},
                        },
                        {
                            "type": "tool_use",
                            "id": "read1",
                            "name": "Read",
                            "input": {"file_path": CANARY["path"]},
                        },
                    ],
                    "usage": _usage(),
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_canary", "content": "done"},
                        {"type": "text", "text": CANARY["prompt"]},
                    ]
                },
                "toolUseResult": {"status": "completed", "agentId": "kidcanary"},
            },
            {
                "type": "attachment",
                "attachment": {
                    "type": "skill_listing",
                    "names": [CANARY["skill"]],
                    "content": f"- {CANARY['skill']}: does things",
                },
            },
            {
                "type": "attachment",
                "attachment": {
                    "type": "deferred_tools_delta",
                    "addedNames": [f"mcp__{CANARY['connector']}__tool"],
                },
            },
            {
                "type": "attachment",
                "attachment": {
                    "type": "instructions",
                    "files": [{"path": CANARY["path"], "content": "x"}],
                },
            },
            {
                "type": "assistant",
                "message": {"model": MODEL, "content": [], "usage": _usage()},
            },
        ],
    )
    packet = (
        '<context-fabric-brief v="0">\n'
        "Files the parent session already opened:\n"
        f"- {CANARY['path']} | 10 lines | read\n"
        "</context-fabric-brief>"
    )
    _write_jsonl(
        root / CANARY["project"] / sid / "subagents" / "agent-kidcanary.jsonl",
        [
            {
                "type": "user",
                "message": {
                    "content": [{"type": "text", "text": CANARY["prompt"] + "\n\n" + packet}]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "model": MODEL,
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "childread1",
                            "name": "Read",
                            "input": {"file_path": CANARY["path"]},
                        }
                    ],
                    "usage": _usage(200, 20),
                },
            },
        ],
    )
    state.mkdir(parents=True, exist_ok=True)
    _write_jsonl(
        state / "briefs.jsonl",
        [
            {
                "session_id": sid,
                "tool_use_id": "toolu_canary",
                "subagent_type": CANARY["agent"],
                "decision": "briefed",
                "skip_reason": None,
                "prompt_bytes": len(CANARY["prompt"].encode("utf-8")),
                "packet_bytes": len(packet.encode("utf-8")),
                "counts": {"files": 1},
                "budget_bytes": 2000,
                "target_model": MODEL,
            }
        ],
    )


def _run(args: list[str], state: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CFX_HOME": str(state)}
    return subprocess.run(
        [sys.executable, "-m", "cfxd", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def _share_commands(root: Path, state: Path) -> list[tuple[list[str], str]]:
    base = ["--root", str(root)]
    return [
        (["diet", *base, "--since", "all", "--share"], "Claude Code diet"),
        (["diet", *base, "--since", "all", "--share", "--json"], '"sessions"'),
        (
            ["week", *base, "--days", "7", "--state-dir", str(state), "--share"],
            "cfxd week",
        ),
        (
            ["week", *base, "--days", "7", "--state-dir", str(state), "--share", "--json"],
            '"buckets"',
        ),
        (
            ["ledger", *base, "--days", "30", "--state-dir", str(state), "--share"],
            "cfxd ledger",
        ),
        (
            ["ledger", *base, "--days", "30", "--state-dir", str(state), "--share", "--json"],
            '"notices"',
        ),
        (
            ["brief", *base, "--state-dir", str(state), "--share"],
            "cfxd brief --share",
        ),
        (
            ["brief", *base, "--state-dir", str(state), "--share", "--json"],
            '"handoffs"',
        ),
    ]


@pytest.mark.parametrize("command,marker", _share_commands(Path("root"), Path("state")))
def test_share_never_leaks_canaries(tmp_path: Path, command: list[str], marker: str) -> None:
    root = tmp_path / "projects"
    state = tmp_path / "state"
    _seed(root, state)
    resolved = [
        str(root) if arg == "root" else str(state) if arg == "state" else arg for arg in command
    ]
    result = _run(resolved, state)

    assert result.returncode == 0, result.stderr
    assert marker in result.stdout
    for canary in CANARY.values():
        assert canary not in result.stdout
        assert canary not in result.stderr


def test_diet_share_json_is_a_golden_numeric_report(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    line = "- golden:skill: desc"
    _write_jsonl(
        root / "proj" / "s1.jsonl",
        [
            {
                "type": "attachment",
                "attachment": {
                    "type": "skill_listing",
                    "names": ["golden:skill"],
                    "content": line,
                },
            },
            {
                "type": "assistant",
                "message": {"content": [], "usage": _usage(400, 0)},
            },
        ],
    )
    result = _run(["diet", "--root", str(root), "--since", "all", "--share", "--json"], tmp_path)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)

    assert data["sessions"] == 1
    assert data["turns"] == 1
    assert data["subagent_type_count"] == 0
    assert data["items"] == [
        {
            "kind": "skill",
            "sessions": 1,
            "median_bytes": float(len(line)),
            "mean_bytes": float(len(line)),
            "used": False,
        }
    ]
    assert data["never_used"]["plugins"] == [
        {
            "count": 1,
            "skill_count": 1,
            "median_bytes": float(len(line)),
            "mean_bytes": float(len(line)),
            "per_turn_tokens": len(line) / 4.0,
            "share": 1.0,
        }
    ]
    assert data["effect"]["per_turn_tokens"] == len(line) / 4.0
    assert data["effect"]["total_tokens"] == len(line) / 4.0
    assert data["delta"]["tokens_per_turn"] == len(line) / 4.0
    assert data["first_turn_tokens_median"] == 400
    assert "golden" not in result.stdout


def test_brief_share_json_is_a_golden_numeric_report(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    state = tmp_path / "state"
    _seed(root, state)
    result = _run(
        ["brief", "--root", str(root), "--state-dir", str(state), "--share", "--json"], state
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)

    assert data["handoffs"] == 1
    assert data["matched"] == 1
    assert data["unmatched"] == 0
    assert data["decisions"] == {"briefed": 1}
    assert data["target_models"] == {MODEL: 1}
    assert data["outcomes"] == {"completed": 1}
    assert data["prompt_bytes"]["total"] == len(CANARY["prompt"].encode("utf-8"))
    assert data["packet_bytes"]["total"] > 0
    assert data["packet_items"] == 1
    assert data["briefed_files"] == 1
    assert data["rereads_of_briefed"] == 1
    assert data["child_tokens"]["total"] == 220
    assert data["child_turns"] == 1
    assert data["compactions"] == 0
    assert data["overflows"] == 0
    assert "digests" not in data
    for canary in CANARY.values():
        assert canary not in result.stdout


def test_share_digests_is_the_only_place_names_appear(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    state = tmp_path / "state"
    _seed(root, state)

    plain = _run(["brief", "--root", str(root), "--state-dir", str(state), "--share"], state)
    flagged = _run(
        ["brief", "--root", str(root), "--state-dir", str(state), "--share-digests", "--json"],
        state,
    )
    assert plain.returncode == flagged.returncode == 0
    for canary in CANARY.values():
        assert canary not in plain.stdout
    assert json.loads(flagged.stdout)["digests"][0]["agent"] == CANARY["agent"]

    diet_plain = _run(["diet", "--root", str(root), "--since", "all", "--share", "--json"], state)
    diet_flagged = _run(
        ["diet", "--root", str(root), "--since", "all", "--share-digests", "--json"], state
    )
    assert "name" not in json.loads(diet_plain.stdout)["items"][0]
    assert json.loads(diet_flagged.stdout)["items"][0]["name"].startswith("skill-")
    for canary in CANARY.values():
        assert canary not in diet_flagged.stdout
