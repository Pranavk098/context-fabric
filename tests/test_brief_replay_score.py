"""R-1 brief replay scorer on synthetic parent/child transcript pairs."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import brief_replay_score as b  # noqa: E402

_USAGE = {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 1000}


def _use(
    mid: str, uid: str, name: str, inp: dict[str, Any], model: str = "claude-sonnet-5-5"
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "id": mid,
            "model": model,
            "usage": _USAGE,
            "content": [{"type": "tool_use", "id": uid, "name": name, "input": inp}],
        },
    }


def _result(uid: str, text: str = "x" * 400, **extra: Any) -> dict[str, Any]:
    block = {"type": "tool_result", "tool_use_id": uid, "content": text}
    return {"type": "user", "message": {"content": [block]}, **extra}


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _pair(
    root: Path,
    session: str,
    reads: list[Path],
    agent_prompt: str,
    child_reads: list[Path],
    model: str,
) -> None:
    parent: list[dict[str, Any]] = []
    for i, path in enumerate(reads):
        parent.extend(
            [
                _use(f"p{i}", f"r{i}", "Read", {"file_path": str(path)}, model="claude-opus-5"),
                _result(f"r{i}"),
            ]
        )
    parent.extend(
        [
            _use("pa", "ag1", "Agent", {"prompt": agent_prompt, "model": model}),
            _result("ag1", toolUseResult={"agentId": "kid1", "status": "completed"}),
        ]
    )
    _write(root / "proj" / f"{session}.jsonl", parent)
    child: list[dict[str, Any]] = []
    for i, path in enumerate(child_reads):
        child.extend(
            [
                _use(f"c{i}", f"t{i}", "Read", {"file_path": str(path)}, model=model),
                _result(f"t{i}"),
            ]
        )
    _write(root / "proj" / session / "subagents" / "agent-kid1.jsonl", child)


def _build(root: Path) -> None:
    # Pair 1: the parent knew both files the child re-read; both fit the packet.
    _pair(
        root,
        "sess1",
        [root / "a.py", root / "b.py"],
        "look around",
        [root / "a.py", root / "b.py", root / "new.py"],
        "claude-sonnet-5-5",
    )
    # Pair 2: ten known files, a Haiku packet holds only the newest eight. The child
    # re-reads the oldest (missed) and the newest (covered).
    known = [root / f"f{i}.py" for i in range(10)]
    _pair(
        root,
        "sess2",
        known,
        "look around",
        [known[0], known[9], root / "z.py"],
        "claude-haiku-4-5",
    )


def test_scores_known_coverage_and_precision(tmp_path: Path) -> None:
    _build(tmp_path)
    data = b.collect(tmp_path, b._load_prices(b._DEFAULT_PRICES))
    kids = {k["session"]: k for k in data["children"]}
    assert set(kids) == {"sess1", "sess2"}

    full = kids["sess1"]
    assert full["parent_known"] == 2
    assert full["redundant"] == 2 and full["covered"] == 2
    assert full["packet_files"] == 2 and full["packet_hit"] == 2
    assert full["packet_bytes"] > 0
    assert full["lower"] is not None and 0 < full["lower"] <= full["upper"] <= full["cost"]
    assert full["result_tokens"] == 200  # two covered results, 400 chars / 4

    partial = kids["sess2"]
    assert partial["family"] == "haiku"
    assert partial["parent_known"] == 10
    assert partial["redundant"] == 2 and partial["covered"] == 1  # oldest file missed
    assert partial["packet_files"] == 8 and partial["packet_hit"] == 1
    assert partial["lower"] is not None and partial["upper"] is not None
    assert partial["result_tokens"] == 100  # only the covered read's result

    all_children = b.summarise(data["children"])
    assert all_children["coverage"] == 3 / 4
    assert all_children["precision"] == 3 / 10


def test_report_has_aggregates_only(tmp_path: Path) -> None:
    _build(tmp_path)
    text = b.render_doc(b.collect(tmp_path, b._load_prices(b._DEFAULT_PRICES)))
    assert "Coverage" in text and "precision" in text
    assert "By child model family" in text
    assert "parent knew 3 or more files" in text
    for leaked in ("a.py", "f0.py", tmp_path.name, str(tmp_path)):
        assert leaked not in text


# --------------------------------------------------------------- budget sweep (E-3)

# Hand-computed golden case. Every known path is "d/" + 80 x's + "NN.py" = 87 chars, so
# one file pointer line is "- " + 87 + " | read" = 96 bytes, plus its newline = 97.
# The fixed part of the rendered block (open tag, 3 prologue lines, files header, close)
# is 532 bytes. At a 2,000 B budget avail = 1,200, so 12 lines fit (12 * 97 = 1,164;
# 13 would be 1,261 > 1,200); at 6,000 B all 14 fit (1,358 bytes).
_GOLDEN_NAME = "d/" + "x" * 80 + "{:02d}.py"
_GOLDEN_LINE = 97
_GOLDEN_FIXED = 532


def _build_sweep(root: Path) -> None:
    known = [_GOLDEN_NAME.format(i) for i in range(14)]
    # Parent read all 14 before the spawn; the child re-reads the two newest (in every
    # packet), the oldest (only in a 6 KB packet) and one unknown file.
    _pair(
        root,
        "gold",
        known,
        "look around",
        [known[13], known[12], known[0], "new.py"],
        "claude-sonnet-5-5",
    )


def test_budget_sweep_golden(tmp_path: Path) -> None:
    _build_sweep(tmp_path)
    prices = b._load_prices(b._DEFAULT_PRICES)
    data = b.collect(tmp_path, prices, (2_000, 6_000))
    (kid,) = data["children"]
    assert kid["parent_known"] == 14

    small = kid["sweep"][2_000]
    assert small["packet_files"] == 12
    assert small["packet_bytes"] == _GOLDEN_FIXED + 12 * _GOLDEN_LINE == 1_696
    assert small["redundant"] == 3 and small["covered"] == 2  # oldest file missed
    assert small["packet_hit"] == 2
    assert small["relevant_bytes"] == 2 * _GOLDEN_LINE == 194

    large = kid["sweep"][6_000]
    assert large["packet_files"] == 14
    assert large["packet_bytes"] == _GOLDEN_FIXED + 14 * _GOLDEN_LINE == 1_890
    assert large["redundant"] == 3 and large["covered"] == 3
    assert large["packet_hit"] == 3
    assert large["relevant_bytes"] == 3 * _GOLDEN_LINE == 291

    # Pooled bytes-weighted precision: relevant bytes / packet bytes.
    assert b.summarise_sweep([small])["bytes_precision"] == 194 / 1_696
    assert b.summarise_sweep([large])["bytes_precision"] == 291 / 1_890

    text = b.render_doc(data)
    assert "| 2,000 B (2 KB) | sonnet | 1 | 66.7% | 16.7% | 11.4% | 1,696 |" in text
    assert "| 6,000 B (6 KB) | sonnet | 1 | 100.0% | 21.4% | 15.4% | 1,890 |" in text
    for leaked in ("xxxx", tmp_path.name, str(tmp_path)):
        assert leaked not in text


def test_sweep_keeps_default_output_and_restores_budget(tmp_path: Path) -> None:
    _build_sweep(tmp_path)
    prices = b._load_prices(b._DEFAULT_PRICES)
    default = b.render_doc(b.collect(tmp_path, prices))
    assert "Budget sweep" not in default
    swept = b.render_doc(b.collect(tmp_path, prices, (2_000, 6_000)))
    assert swept.startswith(default)  # the sweep is appended, nothing above changes
    assert "Budget sweep" in swept
    # The module-level budget is restored after every sweep render.
    assert b.BUDGET_BYTES["sonnet"] == 12_000
