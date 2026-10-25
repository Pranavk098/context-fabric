"""cfxd audit tests: counts-only aggregation of Claude Code transcripts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cfxd.audit import _comp_group_lines, audit, format_human

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


def _json_len(value: object) -> int:
    return len(json.dumps(value).encode("utf-8"))


def test_composition_exact_bytes(tmp_path: Path) -> None:
    hook_line = {
        "type": "attachment",
        "attachment": {"type": "hook_success", "hookEvent": "PreToolUse", "content": "added"},
        "sessionId": "s",
    }
    compact_line = {"type": "system", "subtype": "compact_boundary", "content": "summary text"}
    lines = [
        hook_line,
        compact_line,
        _assistant(
            {"type": "text", "text": "abc"},
            {"type": "thinking", "thinking": "defg"},
            _tool_use("b1", "Bash", command="ls"),
            usage=_usage(10, 2),
        ),
        _user(
            {"type": "text", "text": "hello"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
            },
        ),
    ]
    _session(tmp_path, "proj", "s1", lines)
    cats = audit(tmp_path, "all")["composition"]["parents"]["categories"]

    assert cats["assistant_text"] == {"bytes": 3, "count": 1}
    assert cats["assistant_thinking"] == {"bytes": 4, "count": 1}
    assert cats["tool_use_input"]["bytes"] == _json_len({"command": "ls"})
    assert cats["tool_use_input"]["count"] == 1
    assert cats["tool_use_input"]["by_tool"]["Bash"]["bytes"] == _json_len({"command": "ls"})
    assert cats["user_text"] == {"bytes": 5, "count": 1}
    assert cats["image"] == {"bytes": 4, "count": 1}
    # the largest field of the hook attachment is hookEvent ("PreToolUse"), counted once
    assert cats["hook_additional_context"] == {"bytes": _json_len("PreToolUse"), "count": 1}
    assert cats["compact_summary"] == {"bytes": _json_len(compact_line), "count": 1}
    assert cats["tool_result"] == {"bytes": 0, "count": 0}
    assert cats["system_reminder"] == {"bytes": 0, "count": 0}
    assert cats["other"] == {"bytes": 0, "count": 0, "types": {}}

    total = audit(tmp_path, "all")["composition"]["parents"]["total_bytes"]
    assert total == sum(c["bytes"] for c in cats.values())
    assert audit(tmp_path, "all")["composition"]["parents"]["shares"]["assistant_text"] == (
        pytest.approx(3 / total, abs=5e-5)
    )


def test_composition_tool_use_input_bucketing(tmp_path: Path) -> None:
    lines = [
        _assistant(
            _tool_use("e1", "Edit", file_path="f", old_string="a", new_string="b"),
            _tool_use("w1", "Write", file_path="g", content="c"),
            _tool_use("b1", "Bash", command="ls"),
            _tool_use("a1", "Agent", prompt="p"),
            _tool_use("g1", "Grep", pattern="x"),
            usage=_usage(1, 0),
        ),
    ]
    _session(tmp_path, "proj", "s1", lines)
    bt = audit(tmp_path, "all")["composition"]["parents"]["categories"]["tool_use_input"]["by_tool"]

    assert bt["Edit"]["count"] == 1
    assert bt["Edit"]["bytes"] == _json_len(
        {"file_path": "f", "old_string": "a", "new_string": "b"}
    )
    assert bt["Write"]["count"] == 1
    assert bt["Bash"]["count"] == 1
    assert bt["Agent"]["count"] == 1
    assert bt["other"]["count"] == 1
    assert bt["other"]["bytes"] == _json_len({"pattern": "x"})


def test_composition_system_reminder_no_double_counting(tmp_path: Path) -> None:
    u_span = "<system-reminder>do not forget</system-reminder>"
    r_span = '<system-reminder id="1">note</system-reminder>'
    user_text = "before " + u_span + " after"
    result_text = "x " + r_span + " y"
    lines = [
        _assistant(_tool_use("r1", "Read", file_path="f"), usage=_usage(1, 0)),
        _user(
            {"type": "text", "text": user_text},
            _tool_result("r1", result_text),
        ),
    ]
    _session(tmp_path, "proj", "s1", lines)
    cats = audit(tmp_path, "all")["composition"]["parents"]["categories"]

    assert cats["user_text"]["bytes"] == len(user_text.encode("utf-8")) - len(
        u_span.encode("utf-8")
    )
    assert cats["tool_result"]["bytes"] == len(result_text.encode("utf-8")) - len(
        r_span.encode("utf-8")
    )
    assert cats["system_reminder"]["bytes"] == len(u_span.encode("utf-8")) + len(
        r_span.encode("utf-8")
    )
    assert cats["system_reminder"]["count"] == 2


def test_composition_tool_result_content_shapes(tmp_path: Path) -> None:
    reminder = "<system-reminder>note</system-reminder>"
    lines = [
        _assistant(
            _tool_use("r1", "Read", file_path="f"), _tool_use("r2", "Bash"), usage=_usage(1, 0)
        ),
        _user(
            _tool_result("r1", "plain"),
            _tool_result("r2", [{"type": "text", "text": "a" + reminder + "b"}]),
        ),
    ]
    _session(tmp_path, "proj", "s1", lines)
    cats = audit(tmp_path, "all")["composition"]["parents"]["categories"]

    assert cats["tool_result"]["bytes"] == len("plain") + len("ab")
    assert cats["system_reminder"]["bytes"] == len(reminder)
    assert cats["system_reminder"]["count"] == 1


def test_composition_other_types(tmp_path: Path) -> None:
    lines = [
        {"type": "queue-operation", "operation": "delete"},
        {"type": "attachment", "attachment": {"type": "total_tokens_reminder", "text": "x"}},
        _assistant(_tool_use("b1", "Bash"), usage=_usage(1, 0)),
        _user({"type": "document", "source": {"type": "base64", "data": "abcd"}}),
    ]
    _session(tmp_path, "proj", "s1", lines)
    cats = audit(tmp_path, "all")["composition"]["parents"]["categories"]

    assert cats["other"]["count"] == 2
    assert cats["other"]["types"] == {"queue-operation": 1, "document": 1}
    assert cats["attachment:total_tokens_reminder"]["bytes"] == _json_len("x")
    assert cats["attachment:total_tokens_reminder"]["count"] == 1


def test_composition_attachment_categories(tmp_path: Path) -> None:
    lines = [
        {"type": "attachment", "attachment": {"type": "skill_listing", "content": "a"}},
        {"type": "attachment", "attachment": {"type": "prompt_snapshot", "content": "b"}},
        {"type": "attachment", "attachment": {"type": "instructions", "content": "c"}},
        {"type": "attachment", "attachment": {"type": "skill_listing", "content": "d"}},
        {"type": "attachment", "attachment": {"type": "hook_success", "content": "e"}},
    ]
    _session(tmp_path, "proj", "s1", lines)
    cats = audit(tmp_path, "all")["composition"]["parents"]["categories"]

    assert cats["attachment:skill_listing"]["bytes"] == _json_len("a") + _json_len("d")
    assert cats["attachment:skill_listing"]["count"] == 2
    assert cats["attachment:prompt_snapshot"]["bytes"] == _json_len("b")
    assert cats["attachment:instructions"]["bytes"] == _json_len("c")
    # hook attachments keep their existing hook_additional_context meaning
    assert "attachment:hook_success" not in cats
    assert cats["hook_additional_context"]["bytes"] == _json_len("e")

    total = audit(tmp_path, "all")["composition"]["parents"]["total_bytes"]
    assert total == sum(c["bytes"] for c in cats.values())


def test_composition_attachment_hostile_names(tmp_path: Path) -> None:
    hostile = [
        "C:\\Users\\someone\\secret.txt",  # path-like: backslashes and dots
        "a" * 41,  # over 40 chars
        "héllo-wörld",  # non-ASCII
    ]
    lines = [{"type": "attachment", "attachment": {"type": t, "content": "z"}} for t in hostile]
    _session(tmp_path, "proj", "s1", lines)
    cats = audit(tmp_path, "all")["composition"]["parents"]["categories"]

    expected = len(lines) * _json_len("z")
    assert cats["attachment:other"]["bytes"] == expected
    assert cats["attachment:other"]["count"] == len(lines)
    for t in hostile:
        assert f"attachment:{t}" not in cats


def test_composition_human_top8_cut(tmp_path: Path) -> None:
    canary = "CANARYATTACH-9999"
    lines = [
        {
            "type": "attachment",
            "attachment": {"type": f"att_{i:02d}", "content": "x" * (i + 1) + canary},
        }
        for i in range(12)
    ]
    _session(tmp_path, "proj", "s1", lines)
    group = audit(tmp_path, "all")["composition"]["parents"]
    out = "\n".join(_comp_group_lines("parents", group))

    assert canary not in out
    assert "est. tokens" in out
    # top 8 by bytes are att_11..att_04; smaller and zero-byte base categories are cut
    for i in range(4, 12):
        assert f"attachment:att_{i:02d}:" in out
    for i in range(4):
        assert f"attachment:att_{i:02d}:" not in out
    assert f"other categories: {len(group['categories']) - 8}," in out


def test_composition_json_keeps_attachment_categories(tmp_path: Path) -> None:
    _session(
        tmp_path,
        "proj",
        "s1",
        [
            {
                "type": "attachment",
                "attachment": {"type": "mcp_instructions_delta", "content": "x"},
            },
            {"type": "attachment", "attachment": {"type": "deferred_tools_delta", "content": "y"}},
        ],
    )
    names = ("attachment:mcp_instructions_delta", "attachment:deferred_tools_delta")
    cats = audit(tmp_path, "all")["composition"]["parents"]["categories"]
    for name in names:
        assert name in cats
        assert set(cats[name]) == {"bytes", "count"}

    data = json.loads(json.dumps(audit(tmp_path, "all")))
    for name in names:
        assert name in data["composition"]["parents"]["categories"]


def test_composition_starting_prefix_by_item(tmp_path: Path) -> None:
    s1 = [
        _assistant(usage=_usage(1, 0)),
        {"type": "attachment", "attachment": {"type": "skill_listing", "content": "a"}},
    ]
    s2 = [
        _assistant(usage=_usage(1, 0)),
        {"type": "attachment", "attachment": {"type": "skill_listing", "content": "bb"}},
        {"type": "attachment", "attachment": {"type": "prompt_snapshot", "content": "c"}},
    ]
    _session(tmp_path, "proj", "s1", s1)
    _session(tmp_path, "proj", "s2", s2)
    pg = audit(tmp_path, "all")["composition"]["parents"]
    by_item = pg["starting_prefix_by_item"]

    assert by_item["attachment:skill_listing"]["sessions_present"] == 2
    assert by_item["attachment:prompt_snapshot"]["sessions_present"] == 1
    assert by_item["attachment:skill_listing"]["median_bytes"] == pytest.approx(
        (_json_len("a") + _json_len("bb")) / 2
    )
    assert pg["starting_prefix_by_item_total_median_bytes"] == pytest.approx(
        sum(v["median_bytes"] for v in by_item.values())
    )


def test_composition_attachment_canary_never_leaks(tmp_path: Path) -> None:
    canary = "CANARYATTACH-7777"
    _session(
        tmp_path,
        "proj",
        "s1",
        [{"type": "attachment", "attachment": {"type": "skill_listing", "content": canary}}],
    )
    for extra in ([], ["--json"]):
        result = subprocess.run(
            [sys.executable, "-m", "cfxd", "audit", "--root", str(tmp_path), *extra],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert canary not in result.stdout
        assert canary not in result.stderr


def test_composition_prefix_and_first_turn(tmp_path: Path) -> None:
    _session(
        tmp_path,
        "proj",
        "s1",
        [
            _user({"type": "text", "text": "hi"}),
            _assistant({"type": "text", "text": "abcd"}, usage=_usage(40, 1)),
        ],
    )
    _session(
        tmp_path,
        "proj",
        "s2",
        [
            _user({"type": "text", "text": "yo"}),
            _assistant({"type": "text", "text": "xyz"}, usage=_usage(80, 1)),
        ],
    )
    pg = audit(tmp_path, "all")["composition"]["parents"]

    assert pg["starting_prefix_median_bytes"]["assistant_text"] == pytest.approx(3.5)
    assert pg["whole_session_median_bytes"]["assistant_text"] == pytest.approx(3.5)
    assert pg["starting_prefix_median_bytes"]["user_text"] == pytest.approx(2.0)
    assert pg["first_turn"]["usage_tokens_median"] == pytest.approx(60.0)
    assert pg["first_turn"]["visible_bytes_median"] == pytest.approx(2.0)
    assert pg["first_turn"]["visible_tokens_estimated_median"] == pytest.approx(0.5)
    # 40/(2/4)=80 and 80/(2/4)=160, median 120
    assert pg["first_turn"]["ratio_median"] == pytest.approx(120.0)


def test_composition_subagents_grouped_separately(tmp_path: Path) -> None:
    _session(
        tmp_path,
        "proj",
        "ps",
        [_assistant({"type": "text", "text": "parent"}, usage=_usage(10, 1))],
    )
    _subagent(
        tmp_path,
        "proj",
        "ps",
        "agent-1",
        [_assistant({"type": "text", "text": "child"}, usage=_usage(10, 1))],
    )
    comp = audit(tmp_path, "all")["composition"]

    assert comp["parents"]["categories"]["assistant_text"]["bytes"] == len("parent")
    assert comp["subagents"]["categories"]["assistant_text"]["bytes"] == len("child")


def test_composition_canary_strings_never_leak(tmp_path: Path) -> None:
    canaries = [
        "CANARYREMINDER-0000",
        "CANARYHOOK-1111",
        "CANARYTHINK-2222",
        "CANARYIMG-3333",
        "CANARYCOMPACT-4444",
    ]
    lines = [
        {
            "type": "attachment",
            "attachment": {"type": "hook_success", "content": canaries[1]},
            "sessionId": "s",
        },
        {"type": "system", "subtype": "compact_boundary", "content": canaries[4]},
        _assistant(
            {"type": "text", "text": "hello"},
            {"type": "thinking", "thinking": canaries[2]},
            _tool_use("b1", "Bash", command="x"),
            usage=_usage(1, 0),
        ),
        _user(
            {"type": "text", "text": "note <system-reminder>" + canaries[0] + "</system-reminder>"},
            {"type": "image", "source": {"type": "base64", "data": canaries[3]}},
        ),
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
        for canary in canaries:
            assert canary not in result.stdout
            assert canary not in result.stderr


def test_cli_json_composition(tmp_path: Path) -> None:
    _session(
        tmp_path, "proj", "s1", [_assistant({"type": "text", "text": "hi"}, usage=_usage(10, 2))]
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
    comp = data["composition"]
    assert comp["parents"]["categories"]["assistant_text"]["bytes"] == 2
    assert "shares" in comp["parents"]
    assert "subagents" in comp


def test_cli_human_composition(tmp_path: Path) -> None:
    _session(
        tmp_path, "proj", "s1", [_assistant({"type": "text", "text": "hi"}, usage=_usage(1, 0))]
    )
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "audit", "--root", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "Context composition (parents)" in result.stdout
    assert "Context composition (sub-agents)" in result.stdout


def test_composition_empty_root(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    comp = audit(empty, "all")["composition"]
    assert comp["parents"]["total_bytes"] == 0
    assert comp["subagents"]["total_bytes"] == 0
    assert comp["parents"]["categories"]["other"]["types"] == {}


def test_attachment_bytes_do_not_double_count_names_and_text(tmp_path: Path) -> None:
    # Real attachments store the same entries as a names list and as text; the model sees one.
    text = "x" * 100
    lines = [
        {
            "type": "attachment",
            "attachment": {"type": "skill_listing", "names": ["a", "b"], "content": text},
        },
        {
            "type": "attachment",
            "attachment": {
                "type": "deferred_tools_delta",
                "addedNames": ["mcp__s__t1", "mcp__s__t2"],
                "addedLines": ["mcp__s__t1", "mcp__s__t2"],
            },
        },
    ]
    _session(tmp_path, "proj", "s1", lines)
    cats = audit(tmp_path, "all")["composition"]["parents"]["categories"]
    assert cats["attachment:skill_listing"]["bytes"] == _json_len(text)
    assert cats["attachment:deferred_tools_delta"]["bytes"] == _json_len(
        ["mcp__s__t1", "mcp__s__t2"]
    )
