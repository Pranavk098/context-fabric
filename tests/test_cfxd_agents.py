"""cfxd agent-audit tests: Codex reader and the agent-neutral report."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import cfxd.agents.codex as codex
from cfxd.agents import (
    build_report,
    default_root,
    format_human,
    sanitize_tool_name,
    since_cutoff,
)
from cfxd.agents.codex import read_sessions

ROOT = Path(__file__).resolve().parents[1]

_TS = "2026-01-01T00:00:00Z"


def _line(ordinal: int, type_: str, payload: dict, ts: str = _TS) -> dict:
    return {"timestamp": ts, "ordinal": ordinal, "type": type_, "payload": payload}


def _usage(
    input_tokens: int = 0, cached: int = 0, cw: int = 0, output: int = 0, reasoning: int = 0
) -> dict:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cw,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
    }


def _write(root: Path, name: str, lines: list) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [line if isinstance(line, str) else json.dumps(line) for line in lines]
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return path


def _token_count(ordinal: int, total: dict, info_null: bool = False) -> dict:
    info = None if info_null else {"total_token_usage": total, "last_token_usage": {}}
    return _line(ordinal, "event_msg", {"type": "token_count", "info": info})


def _call(ordinal: int, call_id: str, name: str, custom: bool = False) -> dict:
    kind = "custom_tool_call" if custom else "function_call"
    return _line(ordinal, "response_item", {"type": kind, "call_id": call_id, "name": name})


def _call_output(ordinal: int, call_id: str, output: object, custom: bool = False) -> dict:
    kind = "custom_tool_call_output" if custom else "function_call_output"
    return _line(ordinal, "response_item", {"type": kind, "call_id": call_id, "output": output})


def _session_turns(session_id: str, n: int, source: str = "cli") -> list[dict]:
    lines: list[dict] = [_line(0, "session_meta", {"session_id": session_id, "source": source})]
    o = 1
    for _ in range(n):
        lines.append(_line(o, "event_msg", {"type": "task_started"}))
        lines.append(_token_count(o + 1, _usage(1, 0, 0, 0, 0)))
        lines.append(_line(o + 2, "event_msg", {"type": "task_complete"}))
        o += 3
    return lines


def _run_cli(*args: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cfxd", "agent-audit", "--agent", "codex", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_default_root_codex_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert default_root("codex") == Path.home() / ".codex" / "sessions"
    monkeypatch.setenv("CODEX_HOME", str(Path("C:/custom/codex")))
    assert default_root("codex") == Path("C:/custom/codex") / "sessions"
    with pytest.raises(ValueError):
        default_root("opencode")


def test_since_cutoff() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert since_cutoff("all", now) is None
    assert since_cutoff("7d", now) == now - timedelta(days=7)
    assert since_cutoff("30d", now) == now - timedelta(days=30)


def test_sanitize_tool_name() -> None:
    assert sanitize_tool_name("read_file") == "read_file"
    assert sanitize_tool_name("shell.tool:1") == "shell.tool:1"
    assert sanitize_tool_name("C:\\secret\\tool") == "other"
    assert sanitize_tool_name("tool with spaces") == "other"
    assert sanitize_tool_name("a" * 49) == "other"


def test_cached_input_normalisation_and_duplicate_dedupe(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "rollout-s1.jsonl",
        [
            _line(0, "session_meta", {"session_id": "s1", "source": "cli"}),
            _line(1, "event_msg", {"type": "task_started"}),
            _token_count(2, _usage(1000, 600, 0, 50, 10)),
            _token_count(3, _usage(1500, 900, 100, 80, 15)),
            _token_count(4, _usage(1500, 900, 100, 80, 15)),  # duplicate cumulative
            _line(5, "event_msg", {"type": "task_complete"}),
        ],
    )
    report = build_report("codex", read_sessions(tmp_path, None))
    assert report["turns"]["total"] == 1
    assert report["usage"]["input_uncached"] == 600  # 1500 - 900
    assert report["usage"]["cache_read"] == 900
    assert report["usage"]["cache_write"] == 100
    assert report["usage"]["output"] == 80
    assert report["usage"]["reasoning"] == 15
    # Anthropic-style weighting: 600 + 1.25*100 + 0.1*900 + 5*80 = 1215
    assert report["usage"]["weighted_tokens"] == 1215.0


def test_info_null_skipped(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "rollout-s1.jsonl",
        [
            _line(0, "session_meta", {"session_id": "s1", "source": "cli"}),
            _line(1, "event_msg", {"type": "task_started"}),
            _token_count(2, _usage(), info_null=True),
            _token_count(3, _usage(100, 10, 0, 5, 1)),
            _line(4, "event_msg", {"type": "task_complete"}),
        ],
    )
    report = build_report("codex", read_sessions(tmp_path, None))
    assert report["usage"]["input_uncached"] == 90
    assert report["usage"]["cache_read"] == 10
    assert report["usage"]["output"] == 5
    assert report["usage"]["reasoning"] == 1


def test_function_call_joined_to_output_by_call_id(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "rollout-s1.jsonl",
        [
            _line(0, "session_meta", {"session_id": "s1", "source": "cli"}),
            _line(1, "event_msg", {"type": "task_started"}),
            _call(2, "c1", "read"),
            _call_output(3, "c1", "hello"),
            _call(4, "c2", "shell", custom=True),
            _call_output(5, "c2", "world!", custom=True),
            _call_output(6, "nope", "ignored"),
            _line(7, "event_msg", {"type": "task_complete"}),
        ],
    )
    report = build_report("codex", read_sessions(tmp_path, None))
    assert report["tools"]["read"] == {
        "count": 1,
        "bytes": 5,
        "p50": 5.0,
        "p90": 5.0,
        "max": 5,
        "over_10k": 0,
        "over_25k": 0,
    }
    assert report["tools"]["shell"]["count"] == 1
    assert report["tools"]["shell"]["bytes"] == 6
    assert "nope" not in report["tools"]


def test_unknown_record_types_ignored(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "rollout-s1.jsonl",
        [
            _line(0, "session_meta", {"session_id": "s1", "source": "cli"}),
            _line(1, "event_msg", {"type": "task_started"}),
            _token_count(2, _usage(50, 20, 0, 5, 1)),
            _line(3, "world_state", {"some": "state"}),
            _line(4, "turn_context", {"items": []}),
            _line(5, "response_item", {"type": "message", "role": "user", "content": []}),
            _line(6, "response_item", {"type": "web_search_call", "action": "x"}),
            _line(7, "realtime_item", {}),
            _line(8, "event_msg", {"type": "task_complete"}),
        ],
    )
    report = build_report("codex", read_sessions(tmp_path, None))
    assert report["turns"]["total"] == 1
    assert report["usage"]["input_uncached"] == 30
    assert report["tools"] == {}


def test_malformed_and_huge_lines_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex, "_MAX_LINE_BYTES", 500)
    _write(
        tmp_path,
        "rollout-s1.jsonl",
        [
            _line(0, "session_meta", {"session_id": "s1", "source": "cli"}),
            _line(1, "event_msg", {"type": "task_started"}),
            "this is not json",
            json.dumps(["not", "a", "dict"]),
            _token_count(2, _usage(50, 20, 0, 5, 1)),
            _call(3, "ok", "read"),
            _call_output(4, "ok", "small"),
            _call(5, "big", "shell"),
            json.dumps(_call_output(6, "big", "X" * 1000)),  # > 500 bytes, skipped
            _line(7, "event_msg", {"type": "task_complete"}),
        ],
    )
    report = build_report("codex", read_sessions(tmp_path, None))
    assert report["turns"]["total"] == 1
    assert report["tools"]["read"]["bytes"] == 5
    assert "shell" not in report["tools"]  # the huge output line was skipped


def test_since_filter(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    new = _write(tmp_path, "rollout-new.jsonl", _session_turns("s-new", 1))
    old = _write(tmp_path, "rollout-old.jsonl", _session_turns("s-old", 1))
    os.utime(new, ((now - timedelta(days=2)).timestamp(),) * 2)
    os.utime(old, ((now - timedelta(days=40)).timestamp(),) * 2)

    sessions = list(read_sessions(tmp_path, now - timedelta(days=7)))
    assert [s.session_id for s in sessions] == ["s-new"]
    assert len(list(read_sessions(tmp_path, None))) == 2


def test_cli_since_flag(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    new = _write(tmp_path, "rollout-new.jsonl", _session_turns("s-new", 1))
    old = _write(tmp_path, "rollout-old.jsonl", _session_turns("s-old", 1))
    os.utime(new, ((now - timedelta(days=1)).timestamp(),) * 2)
    os.utime(old, ((now - timedelta(days=40)).timestamp(),) * 2)
    result = _run_cli("--root", str(tmp_path), "--since", "7d", "--json", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["sessions"] == 1


def test_turns_distribution(tmp_path: Path) -> None:
    for sid, n in (("s1", 2), ("s2", 1), ("s3", 31)):
        _write(tmp_path, f"rollout-{sid}.jsonl", _session_turns(sid, n))
    report = build_report("codex", read_sessions(tmp_path, None))
    assert report["sessions"] == 3
    assert report["turns"]["total"] == 34
    assert report["turns"]["median"] == 2.0
    assert report["turns"]["p90"] == pytest.approx(25.2)
    assert report["turns"]["sessions_over_30"] == 1


def test_subagent_detection(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "rollout-sub.jsonl",
        [
            _line(0, "session_meta", {"session_id": "sub1", "source": "subagent"}),
            _line(1, "event_msg", {"type": "task_started"}),
            _line(2, "event_msg", {"type": "task_complete"}),
        ],
    )
    _write(tmp_path, "rollout-top.jsonl", _session_turns("top1", 1))
    report = build_report("codex", read_sessions(tmp_path, None))
    assert report["sessions"] == 1
    assert report["subagent_sessions"] == 1


def test_compaction_detection(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "rollout-c.jsonl",
        [
            _line(0, "session_meta", {"session_id": "c1", "source": "cli"}),
            _line(1, "event_msg", {"type": "task_started"}),
            _line(2, "compacted", {"replacement_history": []}),
            _line(3, "event_msg", {"type": "task_complete"}),
        ],
    )
    assert build_report("codex", read_sessions(tmp_path, None))["compactions"]["count"] == 1


def test_empty_root(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    report = build_report("codex", read_sessions(empty, None))
    assert report["sessions"] == 0
    assert report["subagent_sessions"] == 0
    assert format_human(report, empty.name) == "No codex sessions found under empty."


def test_canary_strings_never_leak(tmp_path: Path) -> None:
    path_canary = "CANARYPATH-1111"
    cmd_canary = "CANARYCMD-2222"
    out_canary = "CANARYOUT-3333"
    prompt_canary = "CANARYPROMPT-4444"
    _write(
        tmp_path,
        "rollout-s1.jsonl",
        [
            _line(
                0,
                "session_meta",
                {
                    "session_id": "s1",
                    "source": "cli",
                    "cwd": f"C:\\u\\{path_canary}",
                    "base_instructions": {"text": prompt_canary},
                },
            ),
            _line(1, "event_msg", {"type": "task_started"}),
            _line(
                2,
                "response_item",
                {"type": "message", "role": "assistant", "content": [{"text": prompt_canary}]},
            ),
            _line(
                3,
                "response_item",
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "apply_patch",
                    "arguments": json.dumps({"path": path_canary, "command": cmd_canary}),
                },
            ),
            _call_output(4, "c1", f"output {out_canary}"),
            _line(5, "event_msg", {"type": "task_complete"}),
        ],
    )
    for extra in ([], ["--json"]):
        result = _run_cli("--root", str(tmp_path), *extra, tmp_path=tmp_path)
        assert result.returncode == 0, result.stderr
        for canary in (path_canary, cmd_canary, out_canary, prompt_canary):
            assert canary not in result.stdout
            assert canary not in result.stderr


def test_cli_subprocess(tmp_path: Path) -> None:
    _write(tmp_path, "rollout-s1.jsonl", _session_turns("s1", 1))
    result = _run_cli("--root", str(tmp_path), "--json", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["agent"] == "codex"
    assert data["sessions"] == 1
    assert data["turns"]["total"] == 1


def test_cli_human_output(tmp_path: Path) -> None:
    _write(tmp_path, "rollout-s1.jsonl", _session_turns("s1", 1))
    result = _run_cli("--root", str(tmp_path), tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "codex sessions" in result.stdout
    assert "This report contains counts and sizes only" in result.stdout


def test_opencode_not_supported(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "agent-audit", "--agent", "opencode"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2
    assert "not supported yet" in result.stderr


def test_decoder_missing_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(codex, "_zstd_open", lambda: None)
    (tmp_path / "rollout-a.jsonl.zst").write_bytes(b"\x28\xb5\x2f\xfd\x00")
    (tmp_path / "rollout-b.jsonl.zst").write_bytes(b"\x28\xb5\x2f\xfd\x00")
    list(codex.read_sessions(tmp_path, None))
    err = capsys.readouterr().err
    assert "2 compressed session(s) skipped" in err
    assert "uvx --with zstandard" in err


def test_zst_file(tmp_path: Path) -> None:
    zstandard = pytest.importorskip("zstandard")
    lines = _session_turns("s1", 1)
    data = ("\n".join(json.dumps(line) for line in lines) + "\n").encode("utf-8")
    (tmp_path / "rollout-z.jsonl.zst").write_bytes(zstandard.ZstdCompressor().compress(data))
    report = build_report("codex", read_sessions(tmp_path, None))
    assert report["sessions"] == 1
    assert report["turns"]["total"] == 1


def test_tool_name_sanitization(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "rollout-t.jsonl",
        [
            _line(0, "session_meta", {"session_id": "s1", "source": "cli"}),
            _line(1, "event_msg", {"type": "task_started"}),
            _call(2, "a", "good_tool-1"),
            _call_output(3, "a", "x"),
            _call(4, "b", "C:\\secret\\path tool"),
            _call_output(5, "b", "y"),
            _line(6, "event_msg", {"type": "task_complete"}),
        ],
    )
    report = build_report("codex", read_sessions(tmp_path, None))
    assert "good_tool-1" in report["tools"]
    assert report["tools"]["other"]["count"] == 1
