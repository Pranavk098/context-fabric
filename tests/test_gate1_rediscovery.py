"""Gate 1 re-discovery script on synthetic transcripts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import gate1_rediscovery as g  # noqa: E402

_USAGE = {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 1000}


def _use(mid: str, uid: str, name: str, inp: dict[str, Any], model: str = "claude-sonnet-5-5"):
    msg = {
        "id": mid,
        "model": model,
        "usage": _USAGE,
        "content": [{"type": "tool_use", "id": uid, "name": name, "input": inp}],
    }
    return {"type": "assistant", "message": msg}


def _result(uid: str, text: str = "x" * 400, **extra: Any) -> dict[str, Any]:
    block = {"type": "tool_result", "tool_use_id": uid, "content": text}
    return {"type": "user", "message": {"content": [block]}, **extra}


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _build(root: Path) -> None:
    proj = root / "proj"
    parent = [
        _use("p1", "r1", "Read", {"file_path": "C:\\src\\a.py"}),
        _result("r1"),
        _use("p2", "b1", "Bash", {"command": "pytest   -q"}),
        _result("b1"),
        _use("p3", "ag1", "Agent", {"prompt": "do it", "run_in_background": True}),
        _result("ag1", toolUseResult={"agentId": "kid1", "status": "async_launched"}),
        _use("p4", "ag2", "Agent", {"prompt": "no result"}),
        _use("p5", "ag3", "Agent", {"prompt": "missing file"}),
        _result("ag3", toolUseResult={"agentId": "ghost", "status": "completed"}),
    ]
    _write(proj / "sess.jsonl", parent)
    # the same message id on two lines must be counted once
    first = _use("c1", "t1", "Read", {"file_path": "c:/src/A.py"})
    text_line = {"type": "assistant", "message": {**first["message"], "content": []}}
    child = [
        first,
        text_line,
        _result("t1"),
        _use("c2", "t2", "Bash", {"command": "pytest -q"}),
        _result("t2"),
        _use("c3", "t3", "Read", {"file_path": "/c/src/new.py"}),
        _result("t3"),
        _use("c4", "t4", "Edit", {"file_path": "/c/src/new.py"}),
        {"type": "user", "isCompactSummary": True, "message": {"content": "s"}},
    ]
    _write(proj / "sess" / "subagents" / "agent-kid1.jsonl", child)
    _write(proj / "sess" / "subagents" / "agent-stray.jsonl", child)


def test_collect_and_summarise(tmp_path: Path) -> None:
    _build(tmp_path)
    prices = g._load_prices(g._DEFAULT_PRICES)
    data = g.collect(tmp_path, prices)
    st = data["stats"]
    assert st["agent_calls"] == 3
    assert st["unjoinable_no_agentid_in_result"] == 1
    assert st["unjoinable_no_child_transcript"] == 1
    assert st["child_files_not_referenced_by_parent"] == 1
    assert data["sessions"]["sess"]["read_before_first"] == 1
    (kid,) = data["children"]
    assert kid["turns"] == 4  # duplicate message id counted once
    assert kid["rereads"] == 1 and kid["reruns"] == 1 and kid["reread_files"] == 1
    assert kid["tool_calls"] == 4
    assert kid["background"] is True
    assert kid["compactions"] == 1
    assert kid["has_edit"] and kid["explore_files"] == 2 and kid["explore_files_known"] == 1
    assert 0 < kid["lower"] <= kid["upper"] <= kid["cost"]
    assert abs(kid["lower"] - kid["cost"] / 2) < 1e-12  # 2 of 4 equal-cost turns
    # exploration ends before the Edit turn (3 of 4 equal turns)
    assert abs(kid["explore_cost"] - kid["cost"] * 0.75) < 1e-12


def test_report_has_no_paths(tmp_path: Path) -> None:
    _build(tmp_path)
    text = g.render(g.collect(tmp_path, g._load_prices(g._DEFAULT_PRICES)))
    assert "Re-discovery share" in text
    assert "a.py" not in text and "src" not in text
