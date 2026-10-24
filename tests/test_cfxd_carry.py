"""cfxd carry tests: roundtrip, isolation, caps, disabled flag, receipts, e2e."""

from __future__ import annotations

import http.client
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from cfxd import session_state as ss
from cfxd.carry import (
    CARRY_DIR,
    CARRY_LOG,
    handle_pre_compact,
    handle_session_start,
    install,
)
from cfxd.config import set_flag
from cfxd.daemon import Daemon
from cfxd.handlers import clear


@pytest.fixture(autouse=True)
def _clean() -> Any:
    ss.reset()
    clear()
    yield
    ss.reset()
    clear()


def _seed_file(session_id: str, path: str, *, edited: bool = False, lines: int | None = 42) -> None:
    tool = "Edit" if edited else "Read"
    ss.observe_post_tool_use(
        {
            "session_id": session_id,
            "tool_name": tool,
            "tool_input": {"file_path": path},
            "tool_response": {"file": {"numLines": lines, "totalLines": lines}},
        }
    )


def _seed_command(session_id: str, command: str) -> None:
    ss.observe_post_tool_use(
        {"session_id": session_id, "tool_name": "Bash", "tool_input": {"command": command}}
    )


def _seed_todos(session_id: str, todos: list[dict[str, str]]) -> None:
    ss.observe_post_tool_use(
        {"session_id": session_id, "tool_name": "TodoWrite", "tool_input": {"todos": todos}}
    )


def _compact(session_id: str, trigger: str = "manual") -> dict[str, Any]:
    return {"session_id": session_id, "trigger": trigger}


def _session_start(session_id: str, source: str = "compact") -> dict[str, Any]:
    return {"session_id": session_id, "source": source}


def _context(result: dict[str, Any]) -> str:
    return result["hookSpecificOutput"]["additionalContext"]


def test_roundtrip_working_set_and_open_todos(tmp_path: Path) -> None:
    _seed_file("s1", "/a/auth.py", edited=True, lines=412)
    _seed_file("s1", "/a/test_auth.py", edited=False, lines=230)
    _seed_todos(
        "s1",
        [
            {"content": "Fix token refresh race", "status": "in_progress"},
            {"content": "write tests", "status": "pending"},
            {"content": "already done", "status": "completed"},
        ],
    )
    handle_pre_compact(_compact("s1"), tmp_path)
    text = _context(handle_session_start(_session_start("s1"), tmp_path))
    assert '<context-fabric-carry v="0" trigger="manual">' in text
    assert "/a/auth.py" in text
    assert "/a/test_auth.py" in text
    assert "in_progress | Fix token refresh race" in text
    assert "pending | write tests" in text
    # completed todos are not "open" and must be left out.
    assert "already done" not in text


def test_source_not_compact_returns_empty(tmp_path: Path) -> None:
    _seed_file("s1", "/a/auth.py", edited=True)
    handle_pre_compact(_compact("s1"), tmp_path)
    assert handle_session_start(_session_start("s1", source="startup"), tmp_path) == {}


def test_carry_session_isolation(tmp_path: Path) -> None:
    _seed_file("A", "/a/only_a.py", edited=True)
    _seed_file("B", "/b/only_b.py", edited=True)
    _seed_file("A", "/a/shared.py", edited=True)
    _seed_command("A", "echo A")
    _seed_command("B", "echo B")
    handle_pre_compact(_compact("A"), tmp_path)

    text = _context(handle_session_start(_session_start("A"), tmp_path))
    assert "/a/only_a.py" in text
    assert "/a/shared.py" in text
    assert "echo A" in text
    assert "/b/only_b.py" not in text
    assert "echo B" not in text

    # A hostile session id must land inside carry/, never escape it.
    _seed_file("../../x", "/c/hostile.py", edited=True)
    handle_pre_compact({"session_id": "../../x", "trigger": "auto"}, tmp_path)
    carry_dir = tmp_path / CARRY_DIR
    names = [p.name for p in carry_dir.glob("*.json")]
    assert names, "hostile-id snapshot should have been written"
    assert all(".." not in name for name in names)
    assert not (tmp_path / "x.json").exists()


def test_snapshot_cap_16k(tmp_path: Path) -> None:
    for i in range(ss.MAX_FILES):
        _seed_file("s1", f"/a/very_long_directory_name_{i:03d}/file_{i:03d}.py", edited=True)
    for i in range(ss.MAX_FOLDS):
        ss.record_fold("s1", "Bash", "s" * 300, f"obs{i:016x}", "/obs/0000.txt", 50_000, "test")
    for i in range(ss.MAX_COMMANDS):
        _seed_command("s1", "c" * 300 + str(i))
    _seed_todos("s1", [{"content": "t" * 300, "status": "pending"} for _ in range(10)])
    handle_pre_compact(_compact("s1"), tmp_path)
    for path in (tmp_path / CARRY_DIR).glob("*.json"):
        assert path.stat().st_size <= 16 * 1024


def test_render_cap_3000(tmp_path: Path) -> None:
    for i in range(20):
        _seed_file("s1", f"/a/f{i}.py", edited=True)
    for i in range(5):
        ss.record_fold("s1", "Bash", "s" * 300, f"obs{i:016x}", "/obs/0000.txt", 50_000, "test")
    for i in range(5):
        _seed_command("s1", "c" * 300 + str(i))
    _seed_todos("s1", [{"content": "t" * 300, "status": "pending"} for _ in range(8)])
    handle_pre_compact(_compact("s1"), tmp_path)
    text = _context(handle_session_start(_session_start("s1"), tmp_path))
    body = text[text.index("<context-fabric-carry") :]
    assert len(body.encode("utf-8")) <= 3000
    assert body.count("</context-fabric-carry>") == 1
    # The working set (top) survives; the bottom sections are dropped first.
    assert "Working set (path | lines | status):" in body


def test_disabled_flag(tmp_path: Path) -> None:
    _seed_file("s1", "/a/auth.py", edited=True)
    set_flag(tmp_path, "carry", False)
    assert handle_pre_compact(_compact("s1"), tmp_path) == {}
    assert not (tmp_path / CARRY_DIR).exists()
    assert handle_session_start(_session_start("s1"), tmp_path) == {}
    assert "disabled" in (tmp_path / CARRY_LOG).read_text(encoding="utf-8")


def test_missing_obs_files_dropped(tmp_path: Path) -> None:
    _seed_file("s1", "/a/auth.py", edited=True)
    ss.record_fold("s1", "Bash", "pytest -q", "deadbeef00000000", "/obs/dead.txt", 50_000, "test")
    handle_pre_compact(_compact("s1"), tmp_path)
    text = _context(handle_session_start(_session_start("s1"), tmp_path))
    assert "pytest -q" not in text
    assert "Saved full outputs" not in text


def test_existing_obs_files_included(tmp_path: Path) -> None:
    _seed_file("s1", "/a/auth.py", edited=True)
    ss.record_fold("s1", "Bash", "pytest -q", "deadbeef00000000", "/obs/dead.txt", 50_000, "test")
    obs = tmp_path / "obs"
    obs.mkdir(parents=True, exist_ok=True)
    (obs / "deadbeef00000000.txt").write_text("x", encoding="utf-8")
    handle_pre_compact(_compact("s1"), tmp_path)
    text = _context(handle_session_start(_session_start("s1"), tmp_path))
    assert "pytest -q" in text
    assert "Saved full outputs" in text


def test_receipt_never_contains_content(tmp_path: Path) -> None:
    canary = "CANARY-UNIQUE-9f8e7d6c5b4a3210"
    _seed_file("s1", "/a/auth.py", edited=True)
    _seed_command("s1", f"echo {canary}")
    _seed_todos("s1", [{"content": f"fix {canary} now", "status": "pending"}])
    handle_pre_compact(_compact("s1"), tmp_path)
    handle_session_start(_session_start("s1"), tmp_path)
    assert canary not in (tmp_path / CARRY_LOG).read_text(encoding="utf-8")


def test_pruning(tmp_path: Path) -> None:
    carry_dir = tmp_path / CARRY_DIR
    carry_dir.mkdir(parents=True, exist_ok=True)
    old = carry_dir / "old.json"
    new = carry_dir / "new.json"
    old.write_text("{}", encoding="utf-8")
    new.write_text("{}", encoding="utf-8")
    stale = time.time() - 8 * 24 * 3600
    os.utime(old, (stale, stale))
    install(tmp_path)
    assert not old.exists()
    assert new.exists()


def _post(port: int, event: str, payload: bytes) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(
            "POST",
            f"/hooks/{event}",
            body=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, json.loads(body) if body else {}
    finally:
        conn.close()


def test_end_to_end_precompact_then_session_start(tmp_path: Path) -> None:
    d = Daemon(tmp_path / "cfx", 0)
    d.start()
    try:
        read_payload = json.dumps(
            {
                "tool_name": "Read",
                "session_id": "sess-1",
                "tool_input": {"file_path": "/a/auth.py"},
                "tool_response": {
                    "type": "text",
                    "file": {"content": "x" * 100, "numLines": 42, "totalLines": 42},
                },
            }
        ).encode("utf-8")
        assert _post(d.port, "PostToolUse", read_payload)[0] == 200
        todo_payload = json.dumps(
            {
                "tool_name": "TodoWrite",
                "session_id": "sess-1",
                "tool_input": {"todos": [{"content": "finish carry", "status": "in_progress"}]},
            }
        ).encode("utf-8")
        assert _post(d.port, "PostToolUse", todo_payload)[0] == 200

        compact_payload = json.dumps({"session_id": "sess-1", "trigger": "auto"}).encode("utf-8")
        status, body = _post(d.port, "PreCompact", compact_payload)
        assert status == 200
        assert body == {}

        start_payload = json.dumps({"session_id": "sess-1", "source": "compact"}).encode("utf-8")
        status, body = _post(d.port, "SessionStart", start_payload)
        assert status == 200
        context = body["hookSpecificOutput"]["additionalContext"]
        assert '<context-fabric-carry v="0" trigger="auto">' in context
        assert "/a/auth.py" in context
        assert "finish carry" in context
    finally:
        d.stop()
        clear()
