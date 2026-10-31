"""Routing's child-model section on synthetic transcripts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import gate1_rediscovery as g  # noqa: E402

from cfxd.routing import _DEFAULT_PRICES, _load_prices, format_human, routing  # noqa: E402

_USAGE = {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 1000}


def _use(mid: str, uid: str, name: str, inp: dict[str, Any], model: str | None = None) -> dict:
    msg: dict[str, Any] = {
        "id": mid,
        "usage": _USAGE,
        "content": [{"type": "tool_use", "id": uid, "name": name, "input": inp}],
    }
    if model is not None:
        msg["model"] = model
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
        _use("p4", "ag2", "Agent", {"prompt": "unpriced child"}),
        _result("ag2", toolUseResult={"agentId": "kid2", "status": "completed"}),
    ]
    _write(proj / "sess.jsonl", parent)

    first = _use("c1", "t1", "Read", {"file_path": "c:/src/A.py"}, model="claude-sonnet-5-5")
    duplicate = {"type": "assistant", "message": {**first["message"], "content": []}}
    child = [
        first,
        duplicate,  # same message id: usage must be counted once
        _result("t1"),
        _use("c2", "t2", "Bash", {"command": "pytest -q"}, model="claude-sonnet-5-5"),
        _result("t2"),
        _use("c3", "t3", "Read", {"file_path": "/c/src/new.py"}, model="claude-sonnet-5-5"),
        _result("t3"),
        _use("c4", "t4", "Edit", {"file_path": "/c/src/new.py"}, model="claude-sonnet-5-5"),
    ]
    _write(proj / "sess" / "subagents" / "agent-kid1.jsonl", child)
    # An assistant turn with no model is priced as unknown, like Gate 1's table.
    unk = [_use("u1", "u1", "Read", {"file_path": "/c/src/other.py"})]
    _write(proj / "sess" / "subagents" / "agent-kid2.jsonl", unk)
    # No parent call points at this child: excluded from the section.
    _write(proj / "sess" / "subagents" / "agent-stray.jsonl", child)


def test_child_models_match_gate1(tmp_path: Path) -> None:
    _build(tmp_path)
    prices = _load_prices(_DEFAULT_PRICES)
    kids = g.collect(tmp_path, prices)["children"]
    report = routing(tmp_path, since="all")

    rows = {row["family"]: row for row in report["child_models"]["families"]}
    assert set(rows) == {kid["family"] for kid in kids}
    for fam, row in rows.items():
        expected = g.summarise([kid for kid in kids if kid["family"] == fam])
        assert row["children"] == expected["n"]
        assert row["priced_children"] == expected["n_priced"]
        assert row["dollars"] == round(expected["cost_total"], 4)
        for key in ("lower_share", "upper_share", "redundant_call_share", "explore_share"):
            if expected[key] is None:
                assert row[key] is None
            else:
                assert row[key] == pytest.approx(expected[key], abs=1e-4)

    sonnet = rows["sonnet"]
    assert sonnet["lower_share"] == pytest.approx(0.5)  # 2 of 4 equal-cost turns
    assert 0.0 < sonnet["upper_share"] <= 1.0  # charged turns plus carried results
    assert sonnet["redundant_call_share"] == pytest.approx(0.5)
    assert sonnet["explore_share"] == pytest.approx(0.75)  # before the Edit

    unknown = rows["unknown"]
    assert unknown["priced_children"] == 0 and unknown["dollars"] == 0.0
    assert unknown["lower_share"] is None and unknown["explore_share"] is None
    assert report["child_models"]["note"] == "price only; quality unmeasured"


def test_human_output_has_no_paths(tmp_path: Path) -> None:
    _build(tmp_path)
    text = format_human(routing(tmp_path, since="all"), "root")
    assert "quality unmeasured" in text
    assert "sonnet: 1 children" in text
    assert "unknown: 1 children, 0 priced" in text
    assert "a.py" not in text and "src" not in text


def test_no_subagents(tmp_path: Path) -> None:
    _write(
        tmp_path / "proj" / "solo.jsonl",
        [_use("p1", "r1", "Read", {"file_path": "C:\\src\\a.py"}), _result("r1")],
    )
    # Too shallow to be a Claude session child: must be ignored, not crash.
    _write(
        tmp_path / "subagents" / "agent-orphan.jsonl",
        [_use("o1", "t1", "Read", {"file_path": "/c/src/x.py"}, model="claude-sonnet-5-5")],
    )
    report = routing(tmp_path, since="all")
    assert report["child_models"]["families"] == []
    assert "no joined child transcripts" in format_human(report, "root")
