"""cfxd fold tests: PostToolUse folding, saved-output lifecycle, and the log."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from cfxd import fold
from cfxd.daemon import Daemon
from cfxd.fold import handle_post_tool_use, install
from cfxd.handlers import clear

_REAL_FOLD_ENABLED = fold._fold_enabled
_REAL_FOLD_READ_ENABLED = fold._fold_read_enabled


@pytest.fixture(autouse=True)
def _fold_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # Folding and Read folding are off by default; most tests here exercise them, so switch them on.
    monkeypatch.setattr(fold, "_fold_enabled", lambda state_dir: True)
    monkeypatch.setattr(fold, "_fold_read_enabled", lambda state_dir: True)


def test_read_is_not_folded_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fold, "_fold_enabled", _REAL_FOLD_ENABLED)
    monkeypatch.setattr(fold, "_fold_read_enabled", _REAL_FOLD_READ_ENABLED)
    state_dir = tmp_path / "cfx"
    content = chr(10).join(f"def f{i}(): return {i}" for i in range(3000))
    # Nothing folds by default.
    assert handle_post_tool_use(_read_payload(content), state_dir) == {}
    assert handle_post_tool_use(_bash_payload(content), state_dir) == {}
    from cfxd.config import set_flag

    # fold on, fold_read still off: Bash folds, Read does not.
    set_flag(state_dir, "fold", True)
    assert handle_post_tool_use(_bash_payload(content), state_dir) != {}
    assert handle_post_tool_use(_read_payload(content), state_dir) == {}
    set_flag(state_dir, "fold_read", True)
    assert handle_post_tool_use(_read_payload(content), state_dir) != {}


def _read_payload(content: str, *, file_path: str = "mod.py", **file_extra: Any) -> dict[str, Any]:
    file: dict[str, Any] = {
        "filePath": file_path,
        "content": content,
        "numLines": 42,
        "startLine": 1,
        "totalLines": 42,
    }
    file.update(file_extra)
    return {
        "tool_name": "Read",
        "tool_input": {"file_path": file_path},
        "tool_response": {"type": "text", "file": file},
        "session_id": "sess-1",
    }


def _bash_payload(stdout: str, stderr: str = "") -> dict[str, Any]:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": "pytest"},
        "tool_response": {"stdout": stdout, "stderr": stderr},
        "session_id": "sess-1",
    }


def _folded_content(result: dict[str, Any]) -> str:
    assert result
    out = result["hookSpecificOutput"]["updatedToolOutput"]
    return out["file"]["content"]


def test_read_py_folds_saves_and_outlines(tmp_path: Path) -> None:
    lines = ["def top():", "    pass"]
    i = 1
    while len("\n".join(lines).encode("utf-8")) < 50_000:
        lines.append(f"def func_{i}():")
        lines.append("    return None")
        i += 1
    content = "\n".join(lines) + "\n"

    result = handle_post_tool_use(_read_payload(content, marker="keep-me"), tmp_path / "cfx")

    folded = _folded_content(result)
    assert folded != content
    assert "[Context Fabric folded this Read output" in folded
    assert "L1: def top():" in folded
    assert "L3: def func_1():" in folded

    obs_id = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    obs_path = tmp_path / "cfx" / "obs" / f"{obs_id}.txt"
    assert obs_path.read_bytes() == content.encode("utf-8")
    assert obs_path.resolve().as_posix() in folded

    updated = result["hookSpecificOutput"]["updatedToolOutput"]
    assert updated["type"] == "text"
    assert updated["file"]["filePath"] == "mod.py"
    assert updated["file"]["numLines"] == 42
    assert updated["file"]["startLine"] == 1
    assert updated["file"]["totalLines"] == 42
    assert updated["file"]["marker"] == "keep-me"


def test_same_content_same_file(tmp_path: Path) -> None:
    content = ("def a():\n    return 1\n" * 2) + ("# pad\n" * 20_000)
    payload = _read_payload(content)
    r1 = handle_post_tool_use(payload, tmp_path / "cfx")
    r2 = handle_post_tool_use(payload, tmp_path / "cfx")
    assert _folded_content(r1) == _folded_content(r2)
    files = list((tmp_path / "cfx" / "obs").glob("*.txt"))
    assert len(files) == 1


def test_no_fold_cases(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    # Below threshold.
    assert handle_post_tool_use(_read_payload("short content"), state_dir) == {}
    # Read with offset or limit.
    big = "x" * 30_000
    payload = _read_payload(big)
    payload["tool_input"]["offset"] = 10
    assert handle_post_tool_use(payload, state_dir) == {}
    payload = _read_payload(big)
    payload["tool_input"]["limit"] = 5
    assert handle_post_tool_use(payload, state_dir) == {}
    # Read of a path inside obs/ (expansion; folding again would loop).
    inside = str(state_dir / "obs" / "saved.txt")
    assert handle_post_tool_use(_read_payload(big, file_path=inside), state_dir) == {}


def test_bash_pytest_failures_and_stderr(tmp_path: Path) -> None:
    stdout = "\n".join(f"collecting test_{i}" for i in range(1500))
    stdout += (
        "\n____ test_bad ____\n"
        "    assert 1 == 2\n"
        "E   assert 1 == 2\n"
        "FAILED tests/test_x.py::test_bad - AssertionError: assert 1 == 2\n"
    )
    stderr = "STDERR-LINE\n" * 2000
    result = handle_post_tool_use(_bash_payload(stdout, stderr), tmp_path / "cfx")

    updated = result["hookSpecificOutput"]["updatedToolOutput"]
    folded_stdout = updated["stdout"]
    assert "FAILED" in folded_stdout
    assert "E   assert 1 == 2" in folded_stdout
    assert "collecting test_0" not in folded_stdout
    assert updated["stderr"] != stderr
    # stdout and stderr each get their own saved file.
    assert len(list((tmp_path / "cfx" / "obs").glob("*.txt"))) == 2


def test_bash_diff_drops_context(tmp_path: Path) -> None:
    parts = ["diff --git a/x.py b/x.py", "--- a/x.py", "+++ b/x.py", "@@ -1,3 +1,3 @@"]
    for i in range(2000):
        parts.append(f" context line {i}")
        parts.append(f"-removed {i}")
        parts.append(f"+added {i}")
    diff = "\n".join(parts)
    result = handle_post_tool_use(_bash_payload(diff), tmp_path / "cfx")

    excerpt = result["hookSpecificOutput"]["updatedToolOutput"]["stdout"]
    assert "+added 0" in excerpt
    assert "-removed 0" in excerpt
    assert " context line 0" not in excerpt


def test_grep_and_webfetch_fold(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    grep_content = "\n".join(f"match line {i}" for i in range(3000))
    grep_payload = {
        "tool_name": "Grep",
        "tool_input": {"pattern": "match"},
        "tool_response": {
            "mode": "content",
            "numFiles": 1,
            "filenames": ["a.txt"],
            "content": grep_content,
            "numLines": 3000,
        },
        "session_id": "sess-1",
    }
    result = handle_post_tool_use(grep_payload, state_dir)
    assert result["hookSpecificOutput"]["updatedToolOutput"]["content"] != grep_content
    assert result["hookSpecificOutput"]["updatedToolOutput"]["filenames"] == ["a.txt"]

    # Grep with no content -> {}; Glob -> {}.
    no_content = {**grep_payload}
    no_content["tool_response"] = {"filenames": ["a.txt"]}
    assert handle_post_tool_use(no_content, state_dir) == {}
    assert handle_post_tool_use({"tool_name": "Glob", "tool_response": {}}, state_dir) == {}

    # WebFetch result folds.
    result_text = "\n".join(f"<p>para {i}</p>" for i in range(3000))
    webfetch_payload = {
        "tool_name": "WebFetch",
        "tool_input": {"url": "https://example.com"},
        "tool_response": {"result": result_text, "url": "https://example.com"},
        "session_id": "sess-1",
    }
    wf = handle_post_tool_use(webfetch_payload, state_dir)
    assert wf["hookSpecificOutput"]["updatedToolOutput"]["result"] != result_text
    assert wf["hookSpecificOutput"]["updatedToolOutput"]["url"] == "https://example.com"


def test_robust_to_bad_payloads(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    cases: list[Any] = [
        {"tool_name": "Edit", "tool_response": {"content": "x" * 30_000}},
        {"tool_name": "Read"},  # missing tool_response
        {"tool_name": "Read", "tool_response": "not a dict"},
        {"tool_name": "Read", "tool_response": {"type": "text", "file": {"content": 12345}}},
        {"tool_name": "Bash", "tool_response": {"stdout": None}},
        "not a dict at all",
        None,
    ]
    for payload in cases:
        assert handle_post_tool_use(payload, state_dir) == {}  # type: ignore[arg-type]


def test_crlf_roundtrip_byte_exact(tmp_path: Path) -> None:
    content = ("def a():\r\n    return 1\r\n" * 2) + ("# pad\r\n" * 20_000)
    handle_post_tool_use(_read_payload(content), tmp_path / "cfx")
    obs_id = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    saved = tmp_path / "cfx" / "obs" / f"{obs_id}.txt"
    assert saved.read_bytes() == content.encode("utf-8")


def test_fold_log_has_no_content(tmp_path: Path) -> None:
    secret = "sekrit-MARKER-1234abcd"
    content = secret + ("# pad\n" * 20_000)
    handle_post_tool_use(_read_payload(content), tmp_path / "cfx")
    log = (tmp_path / "cfx" / "folds.jsonl").read_text(encoding="utf-8")
    assert secret not in log
    entry = json.loads(log.strip().splitlines()[0])
    assert entry["tool_name"] == "Read"
    assert entry["field"] == "file.content"
    assert entry["bytes_in"] == len(content.encode("utf-8"))
    assert entry["obs_id"]


def test_end_to_end_daemon(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    d = Daemon(state_dir, 0)
    d.start()
    try:
        content = "def a():\n    return 1\n" + ("# pad\n" * 20_000)
        payload = json.dumps(_read_payload(content)).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", d.port, timeout=5)
        conn.request(
            "POST",
            "/hooks/PostToolUse",
            body=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        assert resp.status == 200
        updated = body["hookSpecificOutput"]["updatedToolOutput"]
        assert "[Context Fabric folded this Read output" in updated["file"]["content"]
    finally:
        d.stop()
        clear()


def test_prune_removes_old_keeps_fresh(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    obs_dir = state_dir / "obs"
    obs_dir.mkdir(parents=True)
    old = obs_dir / "old.txt"
    fresh = obs_dir / "fresh.txt"
    old.write_text("old", encoding="utf-8")
    fresh.write_text("fresh", encoding="utf-8")
    old_mtime = time.time() - 8 * 24 * 3600
    os.utime(old, (old_mtime, old_mtime))
    install(state_dir)
    assert not old.exists()
    assert fresh.exists()


def test_1mb_bash_folds_under_150ms(tmp_path: Path) -> None:
    lines: list[str] = []
    size = 0
    i = 0
    while size < 1_000_000:
        line = f"line {i}"
        lines.append(line)
        size += len(line) + 1
        i += 1
    content = "\n".join(lines)
    payload = _bash_payload(content)
    start = time.perf_counter()
    result = handle_post_tool_use(payload, tmp_path / "cfx")
    elapsed = time.perf_counter() - start
    assert result
    assert elapsed < 0.15


def test_refold_refreshes_saved_file_age(tmp_path: Path) -> None:
    # A file the model was just pointed at must not be pruned as 8 days old.
    state_dir = tmp_path / "cfx"
    content = "\n".join(f"row {i}" for i in range(5000))
    handle_post_tool_use(_bash_payload(content), state_dir)
    (saved,) = (state_dir / "obs").iterdir()
    old_mtime = time.time() - 8 * 24 * 3600
    os.utime(saved, (old_mtime, old_mtime))
    handle_post_tool_use(_bash_payload(content), state_dir)
    install(state_dir)
    clear()
    assert saved.exists()


def test_code_file_without_definitions_falls_back_to_head_and_tail(tmp_path: Path) -> None:
    content = "\n".join(f"VALUE_{i} = {i}" for i in range(3000))
    result = handle_post_tool_use(_read_payload(content, file_path="data.py"), tmp_path / "cfx")
    text = _folded_content(result)
    assert "the first 40 and last 40 lines" in text
    assert "VALUE_0 = 0" in text
    assert "VALUE_2999 = 2999" in text


def test_partial_read_header_says_which_lines(tmp_path: Path) -> None:
    # Claude Code can return only part of a file; the header must not claim it's the whole file.
    content = "\n".join(f"def f{i}(): return {i}" for i in range(3000))
    payload = _read_payload(content, numLines=3000, startLine=1, totalLines=5000)
    text = _folded_content(handle_post_tool_use(payload, tmp_path / "cfx"))
    assert "lines 1-3,000 of 5,000" in text
