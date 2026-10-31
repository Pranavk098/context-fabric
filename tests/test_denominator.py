"""Golden test for the E-1 denominator script on one synthetic parent + child pair.

Hand-computed on the repo's sonnet-5-5 list price ($2 input, $10 output per Mtok):
one turn of 1,000 input + 1,000 output tokens costs 0.002 + 0.010 = $0.012.

Parent: Read (turn 1) + Agent spawn (turn 2) = 2 x $0.012 = $0.024.
Child: Read of the known file (turn 1, redundant), Read of a new file (turn 2),
Edit (turn 3) = 3 x $0.012 = $0.036. Lower = upper = the one redundant turn,
with a zero-character result so the carried-result term is zero, = $0.012.

Total session = $0.060. Lower and upper are 0.012 / 0.060 = 20.0% of the session,
and 0.012 / 0.036 = 33.3% of the child (Gate 1's denominator). The parent is
0.024 / 0.060 = 40.0% of the session.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import denominator as d  # noqa: E402

_USAGE = {"input_tokens": 1000, "output_tokens": 1000}
_MODEL = "claude-sonnet-5-5"


def _use(mid: str, uid: str, name: str, inp: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "id": mid,
            "model": _MODEL,
            "usage": _USAGE,
            "content": [{"type": "tool_use", "id": uid, "name": name, "input": inp}],
        },
    }


def _result(uid: str, text: str = "x", **extra: Any) -> dict[str, Any]:
    block = {"type": "tool_result", "tool_use_id": uid, "content": text}
    return {"type": "user", "message": {"content": [block]}, **extra}


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _build(root: Path) -> None:
    parent = [
        _use("p1", "r1", "Read", {"file_path": "C:\\src\\a.py"}),
        _result("r1"),
        _use("p2", "ag1", "Agent", {"prompt": "do it"}),
        _result("ag1", toolUseResult={"agentId": "kid1", "status": "completed"}),
    ]
    child = [
        _use("c1", "t1", "Read", {"file_path": "/c/src/a.py"}),
        _result("t1", text=""),  # zero-length result: no carried-result cost
        _use("c2", "t2", "Read", {"file_path": "/c/src/b.py"}),
        _result("t2"),
        _use("c3", "t3", "Edit", {"file_path": "/c/src/b.py"}),
        _result("t3"),
    ]
    _write(root / "proj" / "sess.jsonl", parent)
    _write(root / "proj" / "sess" / "subagents" / "agent-kid1.jsonl", child)


def test_golden_parent_child_split_and_session_shares(tmp_path: Path) -> None:
    _build(tmp_path)
    data = d.collect(tmp_path, d._load_prices(d._DEFAULT_PRICES))

    (row,) = data["rows"]
    assert row["parent_cost"] == pytest.approx(0.024)
    assert row["child_cost"] == pytest.approx(0.036)
    assert row["total"] == pytest.approx(0.060)
    assert row["lower"] == pytest.approx(0.012)
    assert row["upper"] == pytest.approx(0.012)
    assert row["parent_known"] == 1

    s = d.summarise(data["rows"])
    assert s["parent_share"] == pytest.approx(0.4)
    assert s["child_share"] == pytest.approx(0.6)
    assert s["lower_share"] == pytest.approx(1 / 5)
    assert s["upper_share"] == pytest.approx(1 / 5)
    assert s["lower_of_child"] == pytest.approx(1 / 3)
    assert s["lower_q"] == pytest.approx((0.2, 0.2, 0.2))


def test_report_is_aggregates_only(tmp_path: Path) -> None:
    _build(tmp_path)
    text = d.render(d.collect(tmp_path, d._load_prices(d._DEFAULT_PRICES)))
    assert "Re-discovery as a share of total session cost: 20.0% (lower)" in text
    assert "parent share 40.0%" in text
    assert "a.py" not in text and "src" not in text and "do it" not in text
