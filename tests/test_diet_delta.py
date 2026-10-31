"""cfxd diet delta: the deterministic saving from removing never-used prefix items."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cfxd.diet import diet, format_human

ROOT = Path(__file__).resolve().parents[1]
# cfxd/data/prices.json: sonnet-5-5 cache read 0.1, 5m cache write 2.5 USD per Mtok.
READ_PRICE = 0.1
WRITE_PRICE = 2.5


def _usage(
    *, cache_creation: int = 0, cache_read: int = 0, input_tokens: int = 0
) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": 0,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
    }


def _assistant(*blocks: dict, usage: dict[str, int], model: str = "claude-sonnet-5-5") -> dict:
    return {
        "type": "assistant",
        "message": {"content": list(blocks), "usage": usage, "model": model},
    }


def _tool_use(tool_id: str, name: str, **inp: object) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inp}


def _attachment(att: dict) -> dict:
    return {"type": "attachment", "attachment": att}


def _skill_listing(names: list[str], content: str) -> dict:
    return _attachment({"type": "skill_listing", "names": names, "content": content})


def _deferred_tools(names: list[str]) -> dict:
    return _attachment({"type": "deferred_tools_delta", "addedNames": names})


def _mcp_instructions(names: list[str], blocks: list[str]) -> dict:
    return _attachment(
        {"type": "mcp_instructions_delta", "addedNames": names, "addedBlocks": blocks}
    )


def _session(root: Path, session_id: str, lines: list[dict]) -> Path:
    path = root / "proj" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _never_used_bytes(report: dict) -> int:
    return sum(
        it["median_bytes"]
        for it in report["items"]
        if it["kind"] in ("skill", "connector") and it["used"] is False
    )


def test_delta_tokens_equal_the_sum_of_never_used_item_sizes(tmp_path: Path) -> None:
    content = "- pkg:keep: used every day\n- pkg:drop: never used\n"
    lines = [
        _skill_listing(["pkg:keep", "pkg:drop"], content),
        _deferred_tools(["mcp__gone__t1", "mcp__gone__t2"]),
        _mcp_instructions(["gone"], ["server instructions"]),
        _assistant(_tool_use("s1", "Skill", skill="pkg:keep"), usage=_usage(cache_creation=1000)),
        _assistant(usage=_usage(cache_read=5000)),
        _assistant(usage=_usage(cache_read=5000)),
    ]
    _session(tmp_path, "s1", lines)
    report = diet(tmp_path, "all")

    removed = _never_used_bytes(report)
    per_turn = removed / 4.0
    assert report["turns"] == 3
    assert report["delta"]["tokens_per_turn"] == pytest.approx(per_turn, abs=0.1)
    assert report["delta"]["tokens"] == pytest.approx(per_turn * 3, abs=0.1)
    expected_dollars = per_turn * (2 * READ_PRICE + 1 * WRITE_PRICE) / 1_000_000.0
    assert report["delta"]["dollars"] == pytest.approx(round(expected_dollars, 4))
    assert report["delta"]["sessions_with_items"] == 1
    assert report["delta"]["sessions_priced"] == 1


def test_delta_is_exact_per_session(tmp_path: Path) -> None:
    # Only session a carries the dead item: 2 turns pay for it, session b pays nothing.
    used_only = _skill_listing(["pkg:keep"], "- pkg:keep: used\n")
    with_dead = _skill_listing(["pkg:keep", "pkg:drop"], "- pkg:keep: used\n- pkg:drop: dead\n")
    a = [
        with_dead,
        _deferred_tools(["mcp__gone__t1"]),
        _assistant(_tool_use("s1", "Skill", skill="pkg:keep"), usage=_usage(cache_creation=1000)),
        _assistant(usage=_usage(cache_read=5000)),
    ]
    b = [
        used_only,
        _assistant(_tool_use("s2", "Skill", skill="pkg:keep"), usage=_usage(cache_creation=1000)),
        _assistant(usage=_usage(cache_read=5000)),
    ]
    _session(tmp_path, "a", a)
    _session(tmp_path, "b", b)
    report = diet(tmp_path, "all")

    removed = _never_used_bytes(report)
    assert report["delta"]["tokens_per_turn"] == pytest.approx(removed / 4.0, abs=0.1)
    assert report["delta"]["tokens"] == pytest.approx(removed / 4.0 * 2, abs=0.1)
    assert report["delta"]["sessions_with_items"] == 1


def test_human_output_labels_the_saving_an_estimate(tmp_path: Path) -> None:
    lines = [
        _skill_listing(["pkg:keep", "pkg:drop"], "- pkg:keep: used\n- pkg:drop: dead\n"),
        _assistant(_tool_use("s1", "Skill", skill="pkg:keep"), usage=_usage(cache_creation=1000)),
        _assistant(usage=_usage(cache_read=5000)),
    ]
    _session(tmp_path, "s1", lines)
    text = format_human(diet(tmp_path, "all"))

    assert "would remove" in text
    assert "tokens from every turn" in text
    assert "at list price" in text
    assert "estimate" in text


def test_unpriced_model_reports_no_dollars(tmp_path: Path) -> None:
    lines = [
        _skill_listing(["pkg:keep", "pkg:drop"], "- pkg:keep: used\n- pkg:drop: dead\n"),
        _assistant(
            _tool_use("s1", "Skill", skill="pkg:keep"),
            usage=_usage(cache_creation=1000),
            model="claude-mystery-9",
        ),
    ]
    _session(tmp_path, "s1", lines)
    report = diet(tmp_path, "all")

    assert report["delta"]["tokens_per_turn"] > 0
    assert report["delta"]["sessions_priced"] == 0
    assert report["delta"]["dollars"] == 0.0
    text = format_human(report)
    assert "cost not priced" in text


def test_nothing_unused_says_none(tmp_path: Path) -> None:
    lines = [
        _skill_listing(["pkg:keep"], "- pkg:keep: used\n"),
        _assistant(_tool_use("s1", "Skill", skill="pkg:keep"), usage=_usage(cache_creation=1000)),
    ]
    _session(tmp_path, "s1", lines)
    report = diet(tmp_path, "all")

    assert report["delta"]["tokens_per_turn"] == 0
    assert report["delta"]["tokens"] == 0
    assert "nothing unused found" in format_human(report)


def test_cli_json_includes_delta(tmp_path: Path) -> None:
    lines = [
        _skill_listing(["pkg:keep", "pkg:drop"], "- pkg:keep: used\n- pkg:drop: dead\n"),
        _assistant(_tool_use("s1", "Skill", skill="pkg:keep"), usage=_usage(cache_creation=1000)),
    ]
    _session(tmp_path, "s1", lines)
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "diet", "--root", str(tmp_path), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["delta"]["tokens_per_turn"] == pytest.approx(_never_used_bytes(data) / 4.0, abs=0.1)
    assert "estimate" in data["delta"]["basis"]
