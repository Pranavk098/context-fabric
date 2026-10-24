"""cfxd audit tests: counts-only aggregation of Claude Code transcripts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cfxd.audit import audit, format_human

ROOT = Path(__file__).resolve().parents[1]

_KB = 1024


def _usage(input_tokens: int = 0, output_tokens: int = 0, cc: int = 0, cr: int = 0) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cc,
        "cache_read_input_tokens": cr,
    }


def _assistant(*blocks: dict, usage: dict | None = None) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks), "usage": usage or {}}}


def _user(*blocks: dict) -> dict:
    return {"type": "user", "message": {"content": list(blocks)}}


def _tool_use(tool_id: str, name: str, **inp: object) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inp}


def _tool_result(tool_id: str, content: object) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_id, "content": content}


def _write(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def _session(root: Path, project: str, session_id: str, lines: list[dict]) -> Path:
    path = root / project / f"{session_id}.jsonl"
    _write(path, lines)
    return path


def _subagent(root: Path, project: str, session_id: str, agent_id: str, lines: list[dict]) -> Path:
    path = root / project / session_id / "subagents" / f"{agent_id}.jsonl"
    _write(path, lines)
    return path


def _read_turn(tool_id: str, path: str) -> tuple[dict, dict]:
    return _assistant(_tool_use(tool_id, "Read", file_path=path), usage=_usage(10, 1)), _user(
        _tool_result(tool_id, "x" * 10)
    )


def test_turns_distribution(tmp_path: Path) -> None:
    for sid, n in (("s1", 2), ("s2", 1), ("s3", 31)):
        lines = [_assistant(usage=_usage(1, 0)) for _ in range(n)]
        _session(tmp_path, "proj", sid, lines)
    report = audit(tmp_path, "all")
    assert report["sessions"] == 3
    assert report["subagent_sessions"] == 0
    assert report["turns"]["total"] == 34
    assert report["turns"]["median"] == 2.0
    assert report["turns"]["p90"] == pytest.approx(25.2)
    assert report["turns"]["sessions_over_30"] == 1


def test_tool_bytes_and_mcp_collapse(tmp_path: Path) -> None:
    lines = [
        _assistant(_tool_use("r1", "Read", file_path="a.txt"), usage=_usage(1, 0)),
        _user(_tool_result("r1", "A" * 100)),
        _assistant(_tool_use("b1", "Bash", command="echo hi"), usage=_usage(1, 0)),
        _user(_tool_result("b1", "B" * (20 * _KB))),
        _assistant(_tool_use("m1", "mcp__My_Server__do_thing"), usage=_usage(1, 0)),
        _user(_tool_result("m1", "C" * (30 * _KB))),
        _assistant(_tool_use("m2", "mcp__bad name!__do"), usage=_usage(1, 0)),
        _user(_tool_result("m2", "D" * 10)),
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = audit(tmp_path, "all")

    assert set(report["tools"]) == {"Read", "Bash", "mcp__My_Server", "mcp__other"}
    read = report["tools"]["Read"]
    assert read["count"] == 1
    assert read["bytes"] == 100
    assert read["over_10k"] == 0
    assert read["over_25k"] == 0
    bash = report["tools"]["Bash"]
    assert bash["bytes"] == 20 * _KB
    assert bash["over_10k"] == 1
    assert bash["over_25k"] == 0
    mcp = report["tools"]["mcp__My_Server"]
    assert mcp["bytes"] == 30 * _KB
    assert mcp["over_10k"] == 1
    assert mcp["over_25k"] == 1
    assert report["tools"]["mcp__other"]["bytes"] == 10

    # share of total bytes from >10 KB results, by tool
    total = 100 + 20 * _KB + 30 * _KB + 10
    assert report["share_over_10k"]["Bash"] == pytest.approx((20 * _KB) / total, abs=1e-4)
    assert report["share_over_10k"]["mcp__My_Server"] == pytest.approx((30 * _KB) / total, abs=1e-4)
    assert report["share_over_10k"]["Read"] == 0.0


def test_persisted_output_stubs(tmp_path: Path) -> None:
    lines = [
        _assistant(_tool_use("r1", "Read", file_path="a"), usage=_usage(1, 0)),
        _user(_tool_result("r1", "Output too large")),
        _assistant(_tool_use("b1", "Bash"), usage=_usage(1, 0)),
        _user(_tool_result("b1", "result contains persisted-output here")),
        _assistant(_tool_use("r2", "Read", file_path="b"), usage=_usage(1, 0)),
        _user(_tool_result("r2", "Full output saved to disk")),
        _assistant(_tool_use("r3", "Read", file_path="c"), usage=_usage(1, 0)),
        _user(_tool_result("r3", "plain result")),
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = audit(tmp_path, "all")
    po = report["persisted_output"]
    assert po["count"] == 3
    assert po["phrases"]["Output too large"] == 1
    assert po["phrases"]["persisted-output"] == 1
    assert po["phrases"]["Full output saved to"] == 1


def test_repeated_reads_and_unchanged(tmp_path: Path) -> None:
    lines = [
        _assistant(_tool_use("r1", "Read", file_path="p1.txt"), usage=_usage(1, 0)),
        _user(_tool_result("r1", "X" * 100)),
        _assistant(_tool_use("r2", "Read", file_path="p1.txt"), usage=_usage(1, 0)),
        _user(_tool_result("r2", "X" * 200)),
        _assistant(_tool_use("e1", "Edit", file_path="p1.txt"), usage=_usage(1, 0)),
        _user(_tool_result("e1", "ok")),
        _assistant(_tool_use("r3", "Read", file_path="p1.txt"), usage=_usage(1, 0)),
        _user(_tool_result("r3", "X" * 300)),
        _assistant(_tool_use("r4", "Read", file_path="p2.txt"), usage=_usage(1, 0)),
        _user(_tool_result("r4", "File unchanged")),
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = audit(tmp_path, "all")
    rr = report["repeated_reads"]
    assert rr["count"] == 1  # only r2, since e1 reset p1.txt before r3
    read_bytes = 100 + 200 + 300 + len("File unchanged")
    assert rr["repeat_bytes_share"] == pytest.approx(200 / read_bytes, abs=1e-4)
    assert rr["file_unchanged_dedup"] == 1


def test_partial_reads_are_not_repeats(tmp_path: Path) -> None:
    lines = [
        _assistant(
            _tool_use("r1", "Read", file_path="p.txt", offset=10, limit=5), usage=_usage(1, 0)
        ),
        _user(_tool_result("r1", "X" * 10)),
        _assistant(
            _tool_use("r2", "Read", file_path="p.txt", offset=10, limit=5), usage=_usage(1, 0)
        ),
        _user(_tool_result("r2", "X" * 10)),
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = audit(tmp_path, "all")
    assert report["repeated_reads"]["count"] == 0


def test_compactions(tmp_path: Path) -> None:
    lines = [
        _assistant(usage=_usage(1, 0)),
        {"type": "system", "isCompactSummary": True},
        _assistant(usage=_usage(1, 0)),
        _assistant(usage=_usage(1, 0)),
        {"type": "system", "subtype": "compact_boundary"},
        _assistant(usage=_usage(1, 0)),
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = audit(tmp_path, "all")
    c = report["compactions"]
    assert c["count"] == 2
    assert c["sessions_with_compaction"] == 1
    assert c["turns_after_last_median"] == 1.0
    assert c["turns_after_last_p90"] == 1.0


def test_subagents_first_touch(tmp_path: Path) -> None:
    parent_lines = [
        _assistant(_tool_use("r1", "Read", file_path="shared/known.txt"), usage=_usage(100, 10)),
        _user(_tool_result("r1", "content")),
    ]
    _session(tmp_path, "proj", "ps", parent_lines)
    sub_lines = [
        _assistant(_tool_use("t1", "Bash", command="ls"), usage=_usage(100, 10)),
        _user(_tool_result("t1", "bash out")),
        _assistant(_tool_use("t2", "Read", file_path="shared/known.txt"), usage=_usage(50, 5)),
        _user(_tool_result("t2", "read")),
    ]
    _subagent(tmp_path, "proj", "ps", "agent-1", sub_lines)
    never_lines = [
        _assistant(_tool_use("t1", "Bash"), usage=_usage(100, 0)),
        _user(_tool_result("t1", "out")),
    ]
    _subagent(tmp_path, "proj", "ps", "agent-2", never_lines)

    report = audit(tmp_path, "all")
    sa = report["subagents"]
    assert sa["count"] == 2
    # agent-1: 150 before first touch, 225 total; agent-2: 100, never touched
    assert sa["weighted_tokens_total"] == pytest.approx(325.0)
    assert sa["raw"]["input_tokens"] == 250
    assert sa["raw"]["output_tokens"] == 15
    assert sa["first_touch_fraction"]["median"] == pytest.approx(150 / 225, abs=1e-4)
    assert sa["first_touch_fraction"]["mean"] == pytest.approx(150 / 225, abs=1e-4)
    assert sa["never_touched"] == 1


def test_content_shapes_and_malformed(tmp_path: Path) -> None:
    lines = [
        _assistant(_tool_use("r1", "Read", file_path="f"), usage=_usage(1, 0)),
        _user(
            _tool_result(
                "r1", [{"type": "text", "text": "hello"}, {"type": "image", "source": "x"}]
            )
        ),
        "this is not json",
        json.dumps(["not", "a", "dict"]),
        _assistant(_tool_use("b1", "Bash"), usage=_usage(1, 0)),
        _user(_tool_result("b1", "world")),
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = audit(tmp_path, "all")
    assert report["tools"]["Read"]["bytes"] == 5
    assert report["tools"]["Bash"]["bytes"] == 5
    assert report["turns"]["total"] == 2


def test_oversized_line_skipped(tmp_path: Path) -> None:
    path = _session(
        tmp_path,
        "proj",
        "s1",
        [
            _assistant(_tool_use("r1", "Read", file_path="f"), usage=_usage(1, 0)),
            _assistant(_tool_use("r2", "Read", file_path="g"), usage=_usage(1, 0)),
            _user(_tool_result("r2", "ok")),
        ],
    )
    # insert a >20 MB line in the middle (r1's result), which must be skipped
    big = json.dumps(
        {"type": "user", "message": {"content": [_tool_result("r1", "X" * (21 * 1024 * 1024))]}}
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(_assistant(_tool_use("r1", "Read", file_path="f"), usage=_usage(1, 0)))
            + "\n"
        )
        fh.write(big + "\n")
        fh.write(
            json.dumps(_assistant(_tool_use("r2", "Read", file_path="g"), usage=_usage(1, 0)))
            + "\n"
        )
        fh.write(json.dumps(_user(_tool_result("r2", "ok"))) + "\n")
    report = audit(tmp_path, "all")
    assert report["turns"]["total"] == 2
    assert report["tools"]["Read"]["bytes"] == 2  # only r2's result; the 21 MB line was skipped


def test_since_filter_by_mtime(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    new = _session(tmp_path, "proj", "new", [_assistant(usage=_usage(1, 0))])
    old = _session(tmp_path, "proj", "old", [_assistant(usage=_usage(1, 0))])
    os.utime(new, ((now - timedelta(days=2)).timestamp(),) * 2)
    os.utime(old, ((now - timedelta(days=40)).timestamp(),) * 2)

    assert audit(tmp_path, "7d", now=now)["sessions"] == 1
    assert audit(tmp_path, "30d", now=now)["sessions"] == 1
    assert audit(tmp_path, "all", now=now)["sessions"] == 2


def test_empty_root(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    report = audit(empty, "all")
    assert report["sessions"] == 0
    assert report["subagent_sessions"] == 0
    assert format_human(report, empty.name) == "No transcripts found under empty."


def test_canary_strings_never_leak(tmp_path: Path) -> None:
    path_canary = "CANARYPATH-abcd1234"
    cmd_canary = "CANARYCMD-efgh5678"
    out_canary = "CANARYOUT-ijkl9012"
    prompt_canary = "CANARYPROMPT-mnop3456"
    lines = [
        _assistant(
            {"type": "text", "text": prompt_canary},
            _tool_use("r1", "Read", file_path=f"C:\\u\\{path_canary}\\secret.txt"),
            usage=_usage(1, 0),
        ),
        _user(_tool_result("r1", f"file content {out_canary}")),
        _assistant(_tool_use("b1", "Bash", command=f"echo {cmd_canary}"), usage=_usage(1, 0)),
        _user(_tool_result("b1", "done")),
    ]
    _session(tmp_path, "proj", "s1", lines)

    for extra in ([], ["--json"]):
        result = subprocess.run(
            [sys.executable, "-m", "cfxd", "audit", "--root", str(tmp_path), *extra],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        for canary in (path_canary, cmd_canary, out_canary, prompt_canary):
            assert canary not in result.stdout
            assert canary not in result.stderr


def test_cli_json_output(tmp_path: Path) -> None:
    _session(
        tmp_path,
        "proj",
        "s1",
        [
            _assistant(_tool_use("r1", "Read", file_path="a.txt"), usage=_usage(10, 2)),
            _user(_tool_result("r1", "A" * 100)),
        ],
    )
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "audit", "--root", str(tmp_path), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["sessions"] == 1
    assert data["tools"]["Read"]["bytes"] == 100
    assert data["turns"]["total"] == 1


def test_cli_human_output(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", [_assistant(usage=_usage(1, 0))])
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "audit", "--root", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert (
        "This report contains counts and sizes only; no paths, commands or text." in result.stdout
    )


def test_cli_empty_root_exits_0(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "audit", "--root", str(empty)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "No transcripts found under empty."
