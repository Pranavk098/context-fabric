"""cfxd waste tests: loop, retry and rediscovery waste measurement."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cfxd.waste import format_waste, waste

ROOT = Path(__file__).resolve().parents[1]


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


def _tool_result(tool_id: str, content: object, is_error: bool = False) -> dict:
    block: dict[str, object] = {"type": "tool_result", "tool_use_id": tool_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


def _session(root: Path, project: str, session_id: str, lines: list[dict]) -> Path:
    path = root / project / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def test_loop_3_in_10_and_near_miss(tmp_path: Path) -> None:
    # 3 identical Bash calls in a row -> 1 loop, all 3 calls inside.
    loop_lines = [
        _assistant(_tool_use(f"b{i}", "Bash", command="ls"), usage=_usage(1, 0)) for i in range(3)
    ]
    _session(tmp_path, "proj", "loop", loop_lines)

    # 3 identical calls spread over 11 (positions 0, 5, 10) -> no 10-window holds all 3.
    near: list[dict] = []
    for i in range(11):
        if i in (0, 5, 10):
            near.append(_assistant(_tool_use(f"b{i}", "Bash", command="ls"), usage=_usage(1, 0)))
        else:
            near.append(
                _assistant(_tool_use(f"r{i}", "Read", file_path=f"f{i}.txt"), usage=_usage(1, 0))
            )
    _session(tmp_path, "proj", "near", near)

    report = waste(tmp_path, "all")
    lp = report["loops"]
    assert lp["found"] == 1
    assert lp["sessions_with_loops"] == 1
    assert lp["tool_calls_in_loops"] == 3
    # loop session 3 turns, near session 11 turns -> 3/14 of weighted cost
    assert lp["cost_share"] == pytest.approx(3 / 14, abs=1e-4)


def test_retry_streak_3_vs_2(tmp_path: Path) -> None:
    streak3 = []
    for i, cmd in enumerate(("x", "y", "z")):
        streak3.append(_assistant(_tool_use(f"b{i}", "Bash", command=cmd), usage=_usage(1, 0)))
        streak3.append(_user(_tool_result(f"b{i}", "err", is_error=True)))
    _session(tmp_path, "proj", "streak3", streak3)

    streak2 = []
    for i, cmd in enumerate(("a", "b")):
        streak2.append(_assistant(_tool_use(f"b{i}", "Bash", command=cmd), usage=_usage(1, 0)))
        streak2.append(_user(_tool_result(f"b{i}", "err", is_error=True)))
    _session(tmp_path, "proj", "streak2", streak2)

    report = waste(tmp_path, "all")
    rt = report["retries"]
    assert rt["streaks"] == 1
    assert rt["longest"]["median"] == 3.0
    assert rt["longest"]["p90"] == 3.0
    assert rt["longest"]["max"] == 3
    assert rt["cost_share"] == pytest.approx(3 / 5)


def test_retry_streak_requires_same_tool(tmp_path: Path) -> None:
    lines = [
        _assistant(_tool_use("b1", "Bash", command="x"), usage=_usage(1, 0)),
        _user(_tool_result("b1", "err", is_error=True)),
        _assistant(_tool_use("r1", "Read", file_path="f"), usage=_usage(1, 0)),
        _user(_tool_result("r1", "err", is_error=True)),
        _assistant(_tool_use("b2", "Bash", command="y"), usage=_usage(1, 0)),
        _user(_tool_result("b2", "err", is_error=True)),
    ]
    _session(tmp_path, "proj", "s1", lines)
    assert waste(tmp_path, "all")["retries"]["streaks"] == 0


def test_tail_share_arithmetic(tmp_path: Path) -> None:
    for i in range(9):
        _session(tmp_path, "proj", f"small{i}", [_assistant(usage=_usage(1, 0))])
    _session(tmp_path, "proj", "big", [_assistant(usage=_usage(1, 0)) for _ in range(91)])
    tl = waste(tmp_path, "all")["tail"]

    assert tl["top_10pct_cost_share"] == pytest.approx(0.91)
    assert tl["top_20pct_cost_share"] == pytest.approx(0.92)
    assert tl["turns_per_session"]["p50"] == pytest.approx(1.0)
    assert tl["turns_per_session"]["p90"] == pytest.approx(10.0)
    assert tl["turns_per_session"]["p99"] == pytest.approx(82.9)


def test_long_session_first_vs_last_third(tmp_path: Path) -> None:
    # 60 turns with weighted cost t+1: first third mean 10.5, last third mean 50.5.
    lines = [_assistant(usage=_usage(t + 1, 0)) for t in range(60)]
    _session(tmp_path, "proj", "s1", lines)
    tl = waste(tmp_path, "all")["tail"]

    assert tl["long_sessions"] == 1
    assert tl["cost_per_turn_first_third"] == pytest.approx(10.5)
    assert tl["cost_per_turn_last_third"] == pytest.approx(50.5)


def test_discovery_verb_counting(tmp_path: Path) -> None:
    # Order defines "first 5 tool calls": ls, ls, cat, npm, git status, git push, ...
    turns = [
        ("ls", "Bash", {"command": "ls"}),
        ("ls2", "Bash", {"command": "ls -la"}),
        ("cat", "Bash", {"command": "cat foo.txt"}),
        ("npm", "Bash", {"command": "npm install"}),
        ("gs", "Bash", {"command": "git status"}),
        ("gp", "Bash", {"command": "git push"}),
        ("g1", "Glob", {"pattern": "*.py"}),
        ("g2", "Grep", {"pattern": "x"}),
        ("g3", "Grep", {"pattern": "y", "path": "src"}),
        ("r1", "Read", {"file_path": "f"}),
    ]
    lines = [
        _assistant(_tool_use(tid, name, **inp), usage=_usage(1, 0)) for tid, name, inp in turns
    ]
    _session(tmp_path, "proj", "s1", lines)

    verbs = waste(tmp_path, "all")["rediscovery"]["verbs"]
    assert verbs["ls"]["calls"] == 2
    assert verbs["ls"]["first5_share"] == pytest.approx(1.0)
    assert verbs["cat"]["calls"] == 1
    assert verbs["git status"]["calls"] == 1
    assert verbs["Glob"]["calls"] == 1
    assert verbs["Glob"]["first5_share"] == 0.0
    assert verbs["Grep"]["calls"] == 1
    # non-allow-list first words and path-restricted Grep are excluded
    assert "npm" not in verbs
    assert "git push" not in verbs
    assert sum(v["calls"] for v in verbs.values()) == 6
    # ls share: 2 of 10 tool calls
    assert verbs["ls"]["share"] == pytest.approx(0.2)


def test_error_content_shapes(tmp_path: Path) -> None:
    lines = [
        _assistant(_tool_use("b1", "Bash", command="x"), usage=_usage(1, 0)),
        _user(_tool_result("b1", "boom", is_error=True)),
        _assistant(_tool_use("b2", "Bash", command="y"), usage=_usage(1, 0)),
        _user(_tool_result("b2", [{"type": "text", "text": "fail"}], is_error=True)),
        _assistant(_tool_use("b3", "Bash", command="z"), usage=_usage(1, 0)),
        _user(_tool_result("b3", [{"type": "error"}])),
    ]
    _session(tmp_path, "proj", "s1", lines)
    rt = waste(tmp_path, "all")["retries"]
    assert rt["streaks"] == 1
    assert rt["longest"]["max"] == 3


def test_malformed_lines_skipped(tmp_path: Path) -> None:
    lines = [
        _assistant(_tool_use("b1", "Bash", command="ls"), usage=_usage(1, 0)),
        "this is not json",
        json.dumps(["not", "a", "dict"]),
        _assistant(_tool_use("b2", "Bash", command="pwd"), usage=_usage(1, 0)),
    ]
    _session(tmp_path, "proj", "s1", lines)
    verbs = waste(tmp_path, "all")["rediscovery"]["verbs"]
    assert verbs["ls"]["calls"] == 1
    assert verbs["pwd"]["calls"] == 1


def test_huge_line_skipped(tmp_path: Path) -> None:
    path = _session(
        tmp_path, "proj", "s1", [_assistant(usage=_usage(1, 0)), _assistant(usage=_usage(1, 0))]
    )
    big = json.dumps(
        {"type": "user", "message": {"content": [_tool_result("b1", "X" * (21 * 1024 * 1024))]}}
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(_assistant(_tool_use("b1", "Bash", command="ls"), usage=_usage(1, 0))) + "\n"
        )
        fh.write(big + "\n")
        fh.write(
            json.dumps(_assistant(_tool_use("b2", "Bash", command="pwd"), usage=_usage(1, 0)))
            + "\n"
        )
    verbs = waste(tmp_path, "all")["rediscovery"]["verbs"]
    assert verbs["ls"]["calls"] == 1
    assert verbs["pwd"]["calls"] == 1


def test_since_filter(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    new = _session(tmp_path, "proj", "new", [_assistant(usage=_usage(1, 0))])
    old = _session(tmp_path, "proj", "old", [_assistant(usage=_usage(1, 0))])
    os.utime(new, ((now - timedelta(days=2)).timestamp(),) * 2)
    os.utime(old, ((now - timedelta(days=40)).timestamp(),) * 2)

    assert waste(tmp_path, "7d", now=now)["sessions"] == 1
    assert waste(tmp_path, "30d", now=now)["sessions"] == 1
    assert waste(tmp_path, "all", now=now)["sessions"] == 2


def test_empty_root(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    report = waste(empty, "all")
    assert report["sessions"] == 0
    assert format_waste(report, empty.name) == "No transcripts found under empty."


def test_summary_lines(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", [_assistant(usage=_usage(1, 0))])
    out = format_waste(waste(tmp_path, "all"), "proj")
    assert "most expensive 10% of sessions" in out
    assert "loops and retry streaks account for" in out
    assert "rediscovery verbs account for" in out
    assert "(exploratory, one machine)" in out


def test_canary_strings_never_leak(tmp_path: Path) -> None:
    path_canary = "CANARYPATH-abcd1234"
    cmd_canary = "CANARYCMD-efgh5678"
    out_canary = "CANARYOUT-ijkl9012"
    prompt_canary = "CANARYPROMPT-mnop3456"
    loop_cmd = f"ls {cmd_canary}"
    lines = [
        _assistant(
            {"type": "text", "text": prompt_canary},
            _tool_use("r1", "Read", file_path=f"C:\\u\\{path_canary}\\x.txt"),
            usage=_usage(1, 0),
        ),
        _user(_tool_result("r1", f"content {out_canary}")),
    ]
    # three identical Bash commands carrying a canary argument -> a loop; only "ls"
    # (the allow-listed verb) may be output, never the argument or the input hash.
    for i in range(3):
        lines.append(_assistant(_tool_use(f"b{i}", "Bash", command=loop_cmd), usage=_usage(1, 0)))
        lines.append(_user(_tool_result(f"b{i}", "done")))
    _session(tmp_path, "proj", "s1", lines)

    for extra in ([], ["--json"]):
        result = subprocess.run(
            [sys.executable, "-m", "cfxd", "waste", "--root", str(tmp_path), *extra],
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
        [_assistant(_tool_use("b1", "Bash", command="ls"), usage=_usage(1, 0))],
    )
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "waste", "--root", str(tmp_path), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["sessions"] == 1
    assert data["rediscovery"]["verbs"]["ls"]["calls"] == 1
    assert set(data["summary"]) == {"top_10pct", "loops_retries", "rediscovery"}


def test_cli_human_output(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", [_assistant(usage=_usage(1, 0))])
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "waste", "--root", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "Rediscovery verbs:" in result.stdout
    assert "This report contains counts and numbers only" in result.stdout


def test_cli_empty_root_exits_0(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "waste", "--root", str(empty)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "No transcripts found under empty."
