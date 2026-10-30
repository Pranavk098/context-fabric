"""Brief receipts joiner: known-answer fixtures plus one recorded real sub-agent sample."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from cfxd.brief_receipts import BriefReceipt, join_receipts, receipts_to_json

FIXTURES = Path(__file__).parent / "fixtures" / "real_subagent"
SID = "aaaaaaaa-0000-4000-8000-000000000001"

PACKET = (
    '<context-fabric-brief v="0">\n'
    "Background gathered automatically.\n"
    "Files the parent session already opened (path | lines | status):\n"
    "- /repo/a.py | 10 lines | read\n"
    "- /repo/b.py | edited\n"
    "Commands the parent ran recently:\n"
    "- pytest -q\n"
    "</context-fabric-brief>"
)


def _w(path: Path, lines: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")


def _use(uid: str, name: str, **inp: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": uid, "name": name, "input": inp}


def _asst(mid: str, content: list[dict[str, Any]], usage: dict[str, int], **extra: Any):
    msg = {"id": mid, "role": "assistant", "model": "claude-haiku-4-5", "content": content}
    msg["usage"] = usage
    msg.update(extra)
    return {"type": "assistant", "message": msg}


def _usage(i: int, o: int, cr: int, cc: int) -> dict[str, int]:
    return {
        "input_tokens": i,
        "output_tokens": o,
        "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": cc,
    }


def _build(tmp_path: Path, *, link: bool = True, child: bool = True) -> Path:
    """Parent read a.py, c.py, then spawned child; child re-reads a.py and c.py, reads d.py."""
    proj = tmp_path / "projects" / "p"
    parent = [
        {"type": "user", "message": {"role": "user", "content": "go"}},
        _asst(
            "m1",
            [
                _use("r1", "Read", file_path="/repo/a.py"),
                _use("r2", "Read", file_path="C:\\repo\\c.py"),
            ],
            _usage(1, 1, 1, 1),
        ),
        _asst("m2", [_use("toolu_AGENT", "Agent", prompt="x")], _usage(1, 1, 1, 1)),
        # parent reads e.py only AFTER the Agent call: must not count as "already read"
        _asst("m3", [_use("r3", "Read", file_path="/repo/e.py")], _usage(1, 1, 1, 1)),
    ]
    result: dict[str, Any] = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_AGENT", "content": "done"}],
        },
    }
    if link:
        result["toolUseResult"] = {"status": "completed", "agentId": "abc123"}
    parent.insert(3, result)
    _w(proj / f"{SID}.jsonl", parent)
    if child:
        prompt = "do the task\n\n" + PACKET
        kids = [
            {
                "type": "user",
                "isSidechain": True,
                "agentId": "abc123",
                "message": {"role": "user", "content": prompt},
            },
            # one message split over 2 lines: usage repeated, counted once
            _asst("c1", [_use("k1", "Read", file_path="/repo/a.py")], _usage(2, 5, 100, 10)),
            _asst("c1", [_use("k2", "Read", file_path="/repo/c.py")], _usage(2, 7, 100, 10)),
            _asst("c2", [_use("k3", "Read", file_path="/repo/d.py")], _usage(3, 11, 200, 20)),
            {"type": "system", "subtype": "compact_boundary"},
            {
                "type": "assistant",
                "isApiErrorMessage": True,
                "message": {
                    "role": "assistant",
                    "model": "<synthetic>",
                    "content": [{"type": "text", "text": "Prompt is too long"}],
                },
            },
        ]
        for k in kids:
            k.setdefault("agentId", "abc123")
            k.setdefault("isSidechain", True)
        _w(proj / SID / "subagents" / "agent-abc123.jsonl", kids)
    return tmp_path / "projects"


def _briefs(tmp_path: Path, **over: Any) -> Path:
    entry: dict[str, Any] = {
        "ts": "2026-10-30T00:00:00+00:00",
        "session_id": SID,
        "tool_use_id": "toolu_AGENT",
        "subagent_type": "general-purpose",
        "decision": "briefed",
        "skip_reason": None,
        "prompt_bytes": 300,
        "packet_bytes": len(PACKET.encode()),
        "counts": {"files": 2, "folds": 0, "commands": 1},
        "packet_sha16": hashlib.sha256(PACKET.encode()).hexdigest()[:16],
    }
    entry.update(over)
    p = tmp_path / "briefs.jsonl"
    p.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return p


def test_known_answer(tmp_path: Path) -> None:
    root = _build(tmp_path)
    (r,) = join_receipts(_briefs(tmp_path), root)
    assert r.matched and r.join == "tool_use_id" and r.agent_id == "abc123"
    assert r.packet_bytes == len(PACKET.encode()) and r.packet_items == 3
    assert r.target_model == "claude-haiku-4-5"
    assert r.briefed_files == ["/repo/a.py", "/repo/b.py"]
    assert r.rereads_of_briefed == ["/repo/a.py"]
    # c.py: parent read before the call, child read it, not briefed. e.py read after the call.
    assert r.parent_read_not_briefed == ["/repo/c.py"]
    assert r.child_tokens == {
        "input": 5,
        "output": 18,
        "cache_read": 300,
        "cache_creation": 30,
        "total": 353,
    }
    assert r.child_turns == 2
    assert (r.compactions, r.overflows) == (1, 1)
    assert r.outcome == "completed"


def test_json_roundtrip(tmp_path: Path) -> None:
    root = _build(tmp_path)
    data = receipts_to_json(join_receipts(_briefs(tmp_path), root))
    assert json.loads(json.dumps(data)) == data
    assert data[0]["child_tokens"]["total"] == 353


def test_sha_fallback_when_parent_has_no_agent_id(tmp_path: Path) -> None:
    root = _build(tmp_path, link=False)
    (r,) = join_receipts(_briefs(tmp_path), root)
    assert r.matched and r.join == "packet_sha16" and r.agent_id == "abc123"


def test_unmatched_reasons(tmp_path: Path) -> None:
    root = _build(tmp_path, link=False)
    (r,) = join_receipts(_briefs(tmp_path, packet_sha16="0" * 16), root)
    assert not r.matched and r.unmatched_reason == "no_agent_link"
    assert r.packet_bytes == len(PACKET.encode()) and r.outcome == "completed"

    root = _build(tmp_path / "x", child=False)
    (r,) = join_receipts(_briefs(tmp_path), root)
    assert not r.matched and r.unmatched_reason == "no_child_transcript"

    (r,) = join_receipts(_briefs(tmp_path, session_id="nope"), root)
    assert not r.matched and r.unmatched_reason == "no_parent_transcript"

    (r,) = join_receipts(_briefs(tmp_path, session_id=None), root)
    assert not r.matched and r.unmatched_reason == "no_session_id"


def test_tolerates_garbage(tmp_path: Path) -> None:
    root = _build(tmp_path)
    briefs = _briefs(tmp_path)
    briefs.write_text(briefs.read_text() + "not json\n[1]\n{}\n", encoding="utf-8")
    sub = root / "p" / SID / "subagents" / "agent-abc123.jsonl"
    sub.write_text(sub.read_text() + "{broken\n", encoding="utf-8")
    out = join_receipts(briefs, root)
    assert len(out) == 2 and out[0].matched and not out[1].matched
    assert join_receipts(tmp_path / "missing.jsonl", root) == []


def test_explicit_parent_path(tmp_path: Path) -> None:
    root = _build(tmp_path)
    parent = root / "p" / f"{SID}.jsonl"
    (r,) = join_receipts(_briefs(tmp_path), tmp_path / "empty", {SID: parent})
    assert r.matched


def test_skipped_brief_still_joins_by_tool_use_id(tmp_path: Path) -> None:
    root = _build(tmp_path)
    briefs = _briefs(
        tmp_path, decision="skipped", skip_reason="no-state", packet_bytes=0, packet_sha16=None
    )
    (r,) = join_receipts(briefs, root)
    assert r.matched and r.packet_bytes == 0 and r.join == "tool_use_id"


def test_real_subagent_sample(tmp_path: Path) -> None:
    """Recorded (scrubbed) real Claude Code parent + sub-agent transcript pair."""
    sid = "11111111-2222-4333-8444-555555555555"
    root = tmp_path / "projects"
    shutil.copytree(FIXTURES, root / "p")
    briefs = tmp_path / "briefs.jsonl"
    briefs.write_text(
        json.dumps(
            {
                "session_id": sid,
                "tool_use_id": "toolu_01A2VzHZ4ZGjA5Eg3oriZUUq",
                "decision": "briefed",
                "packet_bytes": 0,
                "counts": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (r,) = join_receipts(briefs, root)
    assert isinstance(r, BriefReceipt)
    assert r.matched and r.join == "tool_use_id" and r.agent_id == "a14e1c29f344ead09"
    assert r.target_model == "claude-haiku-4-5-20251001"
    assert r.outcome == "completed"
    assert len(r.child_files_read) == 4
    # the recording predates Brief, so the child saw no packet
    assert r.briefed_files == [] and r.packet_items is None
    assert r.child_tokens == {
        "input": 8,
        "output": 975,
        "cache_read": 122126,
        "cache_creation": 6927,
        "total": 130036,
    }
    assert r.compactions == 0 and r.overflows == 0
