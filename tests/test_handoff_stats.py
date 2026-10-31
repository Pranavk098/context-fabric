"""Golden synthetic parent/child transcripts for ``cfxd.handoff_stats``.

Every expected number is computed below in comments from the list prices in
``cfxd/data/prices.json`` (sonnet-5-5: input 2.0, cache write 5m 2.5, 1h 4.0,
cache read 0.1, output 10.0; opus-5-5: input 4.0, output 20.0) and from Gate 1's
lower/upper attribution.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

import cfxd.handoff_stats as hs

_PRICES = hs._load_prices(hs._DEFAULT_PRICES)

# 1,000 input * $4 + 100 output * $20 = 6,000 -> $0.006 per opus-5-5 message.
_PARENT_USAGE = {"input_tokens": 1000, "output_tokens": 100}
# 100 input * $2 + 200 cache-write-1h * $4 + 1,000 cache read * $0.1 + 50 output * $10
# = 200 + 800 + 100 + 500 = 1,600 -> $0.0016 per sonnet-5-5 turn.
_CHILD_USAGE = {
    "input_tokens": 100,
    "output_tokens": 50,
    "cache_read_input_tokens": 1000,
    "cache_creation_input_tokens": 200,
}


def _assistant(
    mid: str, content: list[dict[str, Any]], model: str, usage: dict[str, Any]
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"id": mid, "model": model, "usage": usage, "content": content},
    }


def _read(uid: str, path: Path, model: str, usage: dict[str, Any]) -> dict[str, Any]:
    block = {"type": "tool_use", "id": uid, "name": "Read", "input": {"file_path": str(path)}}
    return _assistant(uid, [block], model, usage)


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _pair(
    root: Path,
    session: str,
    parent_rows: list[dict[str, Any]],
    child_rows: list[dict[str, Any]],
) -> None:
    _write(root / "proj" / f"{session}.jsonl", parent_rows)
    _write(root / "proj" / session / "subagents" / "agent-kid1.jsonl", child_rows)


def _spawn(
    prompt: str, model: str, *, msg: str = "pa", tid: str = "ag1", kid: str = "kid1"
) -> tuple[dict[str, Any], dict[str, Any]]:
    call = _assistant(
        msg,
        [{"type": "tool_use", "id": tid, "name": "Agent", "input": {"prompt": prompt}}],
        "claude-opus-5-5",
        _PARENT_USAGE,
    )
    result = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tid}]},
        "toolUseResult": {"agentId": kid, "status": "completed"},
    }
    return call, result


def _golden(root: Path) -> tuple[Path, Path, Path, Path]:
    a, b, c, d = (root / "dd" / f"{name}.py" for name in ("a", "b", "c", "d"))
    # One parent message with three reads, then a repeated line for the same message id
    # (Claude Code repeats usage per line; it must be counted once): 3 reads at line 0.
    parent_reads = _assistant(
        "p1",
        [
            {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": str(a)}},
            {"type": "tool_use", "id": "r2", "name": "Read", "input": {"file_path": str(b)}},
            {"type": "tool_use", "id": "r3", "name": "Read", "input": {"file_path": str(c)}},
        ],
        "claude-opus-5-5",
        _PARENT_USAGE,
    )
    spawn, result = _spawn("look at the files", "claude-sonnet-5-5")
    _pair(
        root,
        "sess1",
        [parent_reads, _assistant("p1", [], "claude-opus-5-5", _PARENT_USAGE), spawn, result],
        # c1 appears twice with the same usage (one turn, two reads); c2 is a second turn.
        [
            _read("c1", a, "claude-sonnet-5-5", _CHILD_USAGE),
            _read("c1", d, "claude-sonnet-5-5", _CHILD_USAGE),
            _read("c2", b, "claude-sonnet-5-5", _CHILD_USAGE),
        ],
    )
    return a, b, c, d


def test_golden_counts_shares_and_session_cost(tmp_path: Path) -> None:
    a, _b, _c, _d = _golden(tmp_path)
    data = hs.collect(tmp_path, _PRICES)
    report = hs.summarise(data)

    # Parent: p1 (one deduped message) + pa = 2 * $0.006 = $0.012.
    # Child: c1 (two lines, one message id, two reads) + c2 = 2 turns * $0.0016 = $0.0032.
    # Total session cost = 0.012 + 0.0032 = $0.0152.
    # Lower: c1 pro-rated 1 redundant of 2 calls (0.0016 * 1/2 = 0.0008) plus c2 1/1
    # (0.0016) = $0.0024. Upper: both whole turns = $0.0032 (capped at child cost).
    child = data["children"][0]
    assert child["cost"] == pytest.approx(0.0032)  # 0.0064 without message-id dedupe
    assert child["lower"] == pytest.approx(0.0024)
    assert child["upper"] == pytest.approx(0.0032)
    assert report["cost_share"] == {
        "sessions": 1,
        "excluded": 0,
        "lower": pytest.approx(0.0024 / 0.0152),
        "upper": pytest.approx(0.0032 / 0.0152),
    }

    # Three tool-read calls (a, d, b); a and b were already read by the parent -> 2/3.
    assert report["sessions"] == 1 and report["children"] == 1
    assert report["priced_children"] == 1
    assert [row["family"] for row in report["families"]] == ["haiku", "sonnet", "opus", "other"]
    sonnet = report["families"][1]
    assert sonnet == {
        "family": "sonnet",
        "children": 1,
        "parent_knew_3": 1,
        "read_calls": 3,
        "rereads": 2,
        "rediscovery_share": pytest.approx(2 / 3),
    }
    assert report["families"][0]["children"] == 0

    text = hs.format_report(report)
    assert text.splitlines()[-1] == "safe to paste: counts and percentages only"
    assert "66.7%" in text
    # 0.0024 / 0.0152 = 15.79%; 0.0032 / 0.0152 = 21.05%.
    assert "15.8% (lower) to 21.1% (upper)" in text

    # Packet: all three known files fit every budget, so both redundant reads are covered.
    # One pointer line is "- " + posix path + " | read" plus its newline; a and b have
    # equal-length names, so the two hit lines are the same size.
    shown = Path(str(a)).as_posix()
    line = len(f"- {shown} | read".encode()) + 1
    for budget in (2_000, 6_000, 12_000, 20_000):
        swept = child["sweep"][budget]
        assert swept["redundant"] == 2 and swept["covered"] == 2
        assert swept["relevant_bytes"] == 2 * line
        assert swept["packet_bytes"] > swept["relevant_bytes"]
    assert {row["group"] for row in report["packet_sweep"]} == {"all", "opus/sonnet"}
    assert all(row["coverage"] == 1.0 for row in report["packet_sweep"])


def _two_children(root: Path) -> None:
    """One parent with three known files and two sonnet children."""
    a, b, c, d = (root / "dd" / f"{name}.py" for name in ("a", "b", "c", "d"))
    parent_reads = _assistant(
        "p1",
        [
            {"type": "tool_use", "id": f"r{i}", "name": "Read", "input": {"file_path": str(p)}}
            for i, p in enumerate((a, b, c))
        ],
        "claude-opus-5-5",
        _PARENT_USAGE,
    )
    s1, r1 = _spawn("first", "claude-sonnet-5-5", msg="pa1", tid="ag1", kid="kid1")
    s2, r2 = _spawn("second", "claude-sonnet-5-5", msg="pa2", tid="ag2", kid="kid2")
    _write(root / "proj" / "sess1.jsonl", [parent_reads, s1, r1, s2, r2])
    subs = root / "proj" / "sess1" / "subagents"
    _write(
        subs / "agent-kid1.jsonl",
        [
            _read("c1", a, "claude-sonnet-5-5", _CHILD_USAGE),
            _read("c1", d, "claude-sonnet-5-5", _CHILD_USAGE),
            _read("c2", b, "claude-sonnet-5-5", _CHILD_USAGE),
        ],
    )
    _write(subs / "agent-kid2.jsonl", [_read("c3", c, "claude-sonnet-5-5", _CHILD_USAGE)])


def test_golden_two_children_pooled_shares(tmp_path: Path) -> None:
    _two_children(tmp_path)
    report = hs.summarise(hs.collect(tmp_path, _PRICES))

    # Parent: p1 + pa1 + pa2 = 3 messages * $0.006 = $0.018.
    # kid1: c1 (two calls, one message id, so one turn) + c2 = 2 * $0.0016 = $0.0032.
    # kid2: c3 = $0.0016.
    # Total = 0.018 + 0.0032 + 0.0016 = $0.0228.
    # Lower: c1 has 1 redundant call of 2 (0.0016 * 1/2 = 0.0008), c2 reads b (0.0016),
    # c3 reads c (0.0016) -> $0.0040.
    # Upper: every turn that re-reads, whole: 0.0016 + 0.0016 + 0.0016 = $0.0048.
    assert report["cost_share"] == {
        "sessions": 1,
        "excluded": 0,
        "lower": pytest.approx(0.0040 / 0.0228),
        "upper": pytest.approx(0.0048 / 0.0228),
    }
    assert report["children"] == 2 and report["priced_children"] == 2


def _denominator_module() -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "denominator.py"
    spec = importlib.util.spec_from_file_location("_denominator_for_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agrees_with_denominator_on_the_same_root(tmp_path: Path) -> None:
    _two_children(tmp_path)
    denominator = _denominator_module()
    ours = hs.summarise(hs.collect(tmp_path, _PRICES))["cost_share"]
    theirs = denominator.summarise(denominator.collect(tmp_path, _PRICES)["rows"])
    assert theirs["n"] == ours["sessions"] == 1
    assert theirs["lower_share"] == pytest.approx(ours["lower"])
    assert theirs["upper_share"] == pytest.approx(ours["upper"])


def test_one_unpriced_child_keeps_the_session_in_the_cost_share(tmp_path: Path) -> None:
    known = [tmp_path / f"f{i}.py" for i in range(3)]
    parent_reads = _assistant(
        "p1",
        [
            {"type": "tool_use", "id": f"r{i}", "name": "Read", "input": {"file_path": str(p)}}
            for i, p in enumerate(known)
        ],
        "claude-opus-5-5",
        _PARENT_USAGE,
    )
    s1, r1 = _spawn("priced", "claude-sonnet-5-5", msg="pa1", tid="ag1", kid="kid1")
    s2, r2 = _spawn("unpriced", "claude-unknown-9", msg="pa2", tid="ag2", kid="kid2")
    _write(tmp_path / "proj" / "sess1.jsonl", [parent_reads, s1, r1, s2, r2])
    subs = tmp_path / "proj" / "sess1" / "subagents"
    _write(subs / "agent-kid1.jsonl", [_read("c1", known[0], "claude-sonnet-5-5", _CHILD_USAGE)])
    _write(subs / "agent-kid2.jsonl", [_read("c2", known[1], "claude-unknown-9", _CHILD_USAGE)])

    report = hs.summarise(hs.collect(tmp_path, _PRICES))
    # Parent: p1 + pa1 + pa2 = $0.018; priced kid1 = $0.0016 -> $0.0196.
    # kid2 has no price and is left out of dollars, but the session stays in the share.
    assert report["children"] == 2 and report["priced_children"] == 1
    assert report["cost_share"] == {
        "sessions": 1,
        "excluded": 0,
        "lower": pytest.approx(0.0016 / 0.0196),
        "upper": pytest.approx(0.0016 / 0.0196),
    }


def _canary(root: Path) -> None:
    canary = root / "zzcanary_dir"
    files = [canary / f"zzcanary_{i}.py" for i in range(4)]
    parent_reads = _assistant(
        "p1",
        [
            {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": str(files[0])}},
            {"type": "tool_use", "id": "r2", "name": "Read", "input": {"file_path": str(files[1])}},
            {"type": "tool_use", "id": "r3", "name": "Read", "input": {"file_path": str(files[2])}},
            {"type": "tool_use", "id": "r4", "name": "Read", "input": {"file_path": str(files[3])}},
            {
                "type": "tool_use",
                "id": "b1",
                "name": "Bash",
                "input": {"command": "echo zzcanary_command"},
            },
        ],
        "claude-opus-5-5",
        _PARENT_USAGE,
    )
    spawn, result = _spawn("zzcanary_prompt look at zzcanary_file.py", "claude-sonnet-5-5")
    _pair(
        root,
        "zzcanary_session",
        [parent_reads, spawn, result],
        [
            _read("c1", files[0], "claude-sonnet-5-5", _CHILD_USAGE),
            _read("c1", files[1], "claude-sonnet-5-5", _CHILD_USAGE),
        ],
    )


def test_canary_strings_never_appear_in_text_or_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _canary(tmp_path)
    assert hs.main(["--root", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "zzcanary" not in text.lower()
    assert text.splitlines()[-1] == "safe to paste: counts and percentages only"

    assert hs.main(["--root", str(tmp_path), "--json"]) == 0
    raw = capsys.readouterr().out
    assert "zzcanary" not in raw.lower()
    report = json.loads(raw)
    assert report["safe_to_paste"] == "counts and percentages only"
    assert report["children"] == 1 and report["sessions"] == 1
    # Prompt and command text never leak, but the numbers they produced do.
    assert report["families"][1]["read_calls"] == 2


def test_unpriced_child_is_counted_but_excluded_from_the_cost_share(tmp_path: Path) -> None:
    known = [tmp_path / f"f{i}.py" for i in range(3)]
    parent_reads = _assistant(
        "p1",
        [
            {"type": "tool_use", "id": f"r{i}", "name": "Read", "input": {"file_path": str(p)}}
            for i, p in enumerate(known)
        ],
        "claude-opus-5-5",
        _PARENT_USAGE,
    )
    spawn, result = _spawn("look", "claude-unknown-9")
    _pair(
        tmp_path,
        "sess1",
        [parent_reads, spawn, result],
        [_read("c1", known[0], "claude-unknown-9", _CHILD_USAGE)],
    )
    report = hs.summarise(hs.collect(tmp_path, _PRICES))
    assert report["children"] == 1 and report["priced_children"] == 0
    assert report["families"][3] == {
        "family": "other",
        "children": 1,
        "parent_knew_3": 1,
        "read_calls": 1,
        "rereads": 1,
        "rediscovery_share": 1.0,
    }
    assert report["cost_share"]["sessions"] == 0
    assert report["cost_share"]["excluded"] == 1
    assert report["cost_share"]["lower"] is None
    assert "n/a (no priced session)" in hs.format_report(report)


def test_empty_and_missing_root_exit_zero_with_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert hs.main(["--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == hs._EMPTY
    assert hs.main(["--root", str(tmp_path / "missing")]) == 0
    assert capsys.readouterr().out.strip() == hs._EMPTY
    # A session with an empty subagents directory has nothing to join either.
    (tmp_path / "proj" / "sess1" / "subagents").mkdir(parents=True)
    assert hs.main(["--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == hs._EMPTY
