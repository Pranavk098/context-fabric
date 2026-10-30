"""Usage is counted once per message.id, not once per JSONL content-block line."""

from __future__ import annotations

import json
from pathlib import Path

from cfxd.audit import _scan
from cfxd.waste import _scan as waste_scan
from evals.plugin_ab.usage import scan_transcript


def _line(mid: str, cc: int, block: dict) -> dict:
    return {
        "type": "assistant",
        "message": {
            "id": mid,
            "model": "claude-sonnet-4-5",
            "content": [block],
            "usage": {
                "input_tokens": 1,
                "output_tokens": 10,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": cc,
            },
        },
    }


def _tool(i: str) -> dict:
    return {"type": "tool_use", "id": i, "name": "Bash", "input": {"command": f"echo {i}"}}


def _write(tmp_path: Path, objs: list[dict]) -> Path:
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(o) for o in objs) + "\n", encoding="utf-8")
    return path


def _tax_step(tmp_path: Path) -> Path:
    # The off "tax" step: 27,630 = 1035*2 + 5704*4 + 2744 summed per line;
    # three messages (2, 4 and 1 lines), so the deduped truth is 1035 + 5704 + 2744 = 9,483.
    text = {"type": "text", "text": "x"}
    objs = [_line("m1", 1035, text), _line("m1", 1035, _tool("t1"))]
    objs += [_line("m2", 5704, _tool(f"u{i}")) for i in range(4)]
    objs += [_line("m3", 2744, text)]
    return _write(tmp_path, objs)


def test_scan_transcript_counts_cache_writes_once_per_message(tmp_path: Path) -> None:
    result = scan_transcript(_tax_step(tmp_path), {})
    assert result.cache_creation_input_tokens == 9_483  # not 27,630
    assert result.turns == 3
    assert result.cache_read_input_tokens == 300
    assert result.output_tokens == 30


def test_last_line_usage_wins_for_a_message(tmp_path: Path) -> None:
    text = {"type": "text", "text": "x"}
    first, last = _line("m1", 7, text), _line("m1", 7, _tool("t1"))
    first["message"]["usage"]["output_tokens"] = 2
    last["message"]["usage"]["output_tokens"] = 40
    result = scan_transcript(_write(tmp_path, [first, last]), {})
    assert result.output_tokens == 40
    assert result.turns == 1


def test_lines_without_message_id_still_count_each(tmp_path: Path) -> None:
    objs = [_line("m1", 5, {"type": "text", "text": "x"}) for _ in range(2)]
    for o in objs:
        del o["message"]["id"]
    assert scan_transcript(_write(tmp_path, objs), {}).turns == 2


def test_audit_scan_counts_turns_once_per_message(tmp_path: Path) -> None:
    rec = _scan(_tax_step(tmp_path), known=None)
    assert rec["turns"] == 3
    assert rec["raw_cc"] == 9_483


def test_waste_scan_counts_turns_once_and_splits_cost_across_tool_uses(tmp_path: Path) -> None:
    session = waste_scan(_tax_step(tmp_path))
    assert len(session.turns) == 3
    assert len(session.calls) == 5
