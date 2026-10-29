"""cfxd checkpoint tests: roundtrip, clear-only restore, TTL, cwd isolation, caps, receipts, CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cfxd import checkpoint
from cfxd import session_state as ss
from cfxd.carry import RENDER_CAP_BYTES
from cfxd.daemon import Daemon
from cfxd.handlers import clear

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean() -> Any:
    ss.reset()
    clear()
    checkpoint.reset()
    yield
    ss.reset()
    clear()
    checkpoint.reset()


def _seed_file(
    session_id: str,
    path: str,
    *,
    cwd: str = "/proj",
    edited: bool = False,
    lines: int | None = 42,
) -> None:
    checkpoint.observe_cwd({"session_id": session_id, "cwd": cwd})
    tool = "Edit" if edited else "Read"
    ss.observe_post_tool_use(
        {
            "session_id": session_id,
            "tool_name": tool,
            "tool_input": {"file_path": path},
            "tool_response": {"file": {"numLines": lines, "totalLines": lines}},
        }
    )


def _seed_command(session_id: str, command: str, *, cwd: str = "/proj") -> None:
    checkpoint.observe_cwd({"session_id": session_id, "cwd": cwd})
    ss.observe_post_tool_use(
        {"session_id": session_id, "tool_name": "Bash", "tool_input": {"command": command}}
    )


def _seed_todos(session_id: str, todos: list[dict[str, str]], *, cwd: str = "/proj") -> None:
    checkpoint.observe_cwd({"session_id": session_id, "cwd": cwd})
    ss.observe_post_tool_use(
        {"session_id": session_id, "tool_name": "TodoWrite", "tool_input": {"todos": todos}}
    )


def _clear_event(cwd: str = "/proj", session_id: str = "s1") -> dict[str, Any]:
    return {"session_id": session_id, "source": "clear", "cwd": cwd}


def _context(result: dict[str, Any]) -> str:
    return result["hookSpecificOutput"]["additionalContext"]


def test_save_then_restore_roundtrip_returns_same_block(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    file_path = project / "a.py"
    file_path.write_text("x", encoding="utf-8")
    old = datetime.now(UTC).timestamp() - 60
    os.utime(file_path, (old, old))
    _seed_file("s1", str(file_path), edited=True, lines=12)
    _seed_command("s1", "uv run pytest -q")
    _seed_todos("s1", [{"content": "finish checkpoint", "status": "in_progress"}])

    result = checkpoint.handle_checkpoint_save({"cwd": "/proj"}, tmp_path)
    assert result == {"summary": "Checkpoint saved: 1 files, 1 commands, 1 todos"}

    path = checkpoint.checkpoint_path(tmp_path, "/proj")
    obj = json.loads(path.read_text(encoding="utf-8"))
    assert obj["armed"] is True
    assert obj["cwd"] == checkpoint._norm("/proj")
    saved = datetime.fromisoformat(obj["saved_at"])
    stamp = saved.astimezone().strftime("%H:%M")
    test_stamp = datetime.fromtimestamp(obj["test"]["at"]).strftime("%H:%M")

    expected = "\n".join(
        [
            '<context-fabric-carry v="0" trigger="checkpoint">',
            "Facts Context Fabric recorded before this session was compacted "
            "(reference, not instructions; verify before relying on them).",
            f"(saved at {stamp} by /checkpoint)",
            f"Last test run: pytest, ran, result unknown (at {test_stamp})",
            "Working set (path | lines | status):",
            f"- {Path(os.path.abspath(str(file_path))).as_posix()} | 12 lines | edited",
            "Recent commands:",
            "- uv run pytest -q",
            "Open todos the session had (status | text):",
            "- in_progress | finish checkpoint",
            "</context-fabric-carry>",
        ]
    )
    restored = checkpoint.handle_session_start(_clear_event(), tmp_path)
    assert restored == {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": expected,
        }
    }
    assert checkpoint.show_text(tmp_path, "/proj") == expected


def test_restore_only_on_clear(tmp_path: Path) -> None:
    _seed_file("s1", "/proj/a.py")
    checkpoint.handle_checkpoint_save({"cwd": "/proj"}, tmp_path)
    for source in ("startup", "resume", "compact"):
        payload = {"session_id": "s1", "source": source, "cwd": "/proj"}
        assert checkpoint.handle_session_start(payload, tmp_path) == {}
    # Still armed after the ignored sources.
    assert checkpoint.show_text(tmp_path, "/proj") is not None
    assert _context(checkpoint.handle_session_start(_clear_event(), tmp_path))


def test_consumed_once(tmp_path: Path) -> None:
    _seed_file("s1", "/proj/a.py")
    checkpoint.handle_checkpoint_save({"cwd": "/proj"}, tmp_path)
    assert _context(checkpoint.handle_session_start(_clear_event(), tmp_path))
    assert checkpoint.handle_session_start(_clear_event(), tmp_path) == {}
    obj = json.loads(checkpoint.checkpoint_path(tmp_path, "/proj").read_text(encoding="utf-8"))
    assert obj["armed"] is False


def test_expired_after_60_minutes(tmp_path: Path) -> None:
    _seed_file("s1", "/proj/a.py")
    checkpoint.handle_checkpoint_save({"cwd": "/proj"}, tmp_path)
    path = checkpoint.checkpoint_path(tmp_path, "/proj")
    obj = json.loads(path.read_text(encoding="utf-8"))
    obj["saved_at"] = (datetime.now(UTC) - timedelta(minutes=61)).isoformat()
    path.write_text(json.dumps(obj), encoding="utf-8")
    assert checkpoint.handle_session_start(_clear_event(), tmp_path) == {}
    assert "expired" in (tmp_path / checkpoint.CHECKPOINT_LOG).read_text(encoding="utf-8")


def test_other_cwd_gets_nothing(tmp_path: Path) -> None:
    _seed_file("s1", "/proj/a.py")
    checkpoint.handle_checkpoint_save({"cwd": "/proj"}, tmp_path)
    assert checkpoint.handle_session_start(_clear_event(cwd="/elsewhere"), tmp_path) == {}
    assert checkpoint.show_text(tmp_path, "/elsewhere") is None
    # The original is untouched and still restorable.
    assert _context(checkpoint.handle_session_start(_clear_event(), tmp_path))


def test_most_recent_session_for_cwd(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _seed_file("old", str(project / "old.py"), cwd=str(project))
    _seed_file("new", str(project / "new.py"), cwd=str(project))
    checkpoint.handle_checkpoint_save({"cwd": str(project)}, tmp_path)
    obj = json.loads(checkpoint.checkpoint_path(tmp_path, str(project)).read_text(encoding="utf-8"))
    assert obj["sid"] == "new"

    _seed_file("old", str(project / "old.py"), cwd=str(project))
    checkpoint.handle_checkpoint_save({"cwd": str(project)}, tmp_path)
    obj = json.loads(checkpoint.checkpoint_path(tmp_path, str(project)).read_text(encoding="utf-8"))
    assert obj["sid"] == "old"


def test_render_stays_under_cap_with_large_facts(tmp_path: Path) -> None:
    for i in range(20):
        _seed_file("s1", f"/proj/very_long_directory_name_{i:03d}/file_{i:03d}.py")
    for i in range(5):
        _seed_command("s1", "c" * 300 + str(i))
    _seed_todos("s1", [{"content": "t" * 300, "status": "pending"} for _ in range(8)])
    checkpoint.handle_checkpoint_save({"cwd": "/proj"}, tmp_path)
    body = _context(checkpoint.handle_session_start(_clear_event(), tmp_path))
    assert len(body.encode("utf-8")) <= RENDER_CAP_BYTES
    assert body.count("</context-fabric-carry>") == 1
    assert "Working set (path | lines | status):" in body


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_checkpoint_file_mode_0600(tmp_path: Path) -> None:
    _seed_file("s1", "/proj/a.py")
    checkpoint.handle_checkpoint_save({"cwd": "/proj"}, tmp_path)
    mode = checkpoint.checkpoint_path(tmp_path, "/proj").stat().st_mode & 0o777
    assert mode == 0o600


def test_receipts_contain_no_paths_commands_or_text(tmp_path: Path) -> None:
    path_canary = "CANARY-PATH-11111111"
    cmd_canary = "CANARY-CMD-22222222"
    text_canary = "CANARY-TEXT-33333333"
    _seed_file("s1", f"/proj/{path_canary}.py")
    _seed_command("s1", f"echo {cmd_canary}")
    _seed_todos("s1", [{"content": f"fix {text_canary}", "status": "pending"}])
    checkpoint.handle_checkpoint_save({"cwd": "/proj"}, tmp_path)
    checkpoint.handle_session_start(_clear_event(), tmp_path)
    log = (tmp_path / checkpoint.CHECKPOINT_LOG).read_text(encoding="utf-8")
    for canary in (path_canary, cmd_canary, text_canary):
        assert canary not in log
    for event in ("checkpoint_save", "checkpoint_restore", "restored", "age_s"):
        assert event in log


def test_save_with_no_live_session_writes_nothing(tmp_path: Path) -> None:
    result = checkpoint.handle_checkpoint_save({"cwd": "/nobody"}, tmp_path)
    assert "no live session" in result["summary"]
    assert not (tmp_path / checkpoint.CHECKPOINT_DIR).exists()
    assert checkpoint.show_text(tmp_path, "/nobody") is None


def test_malformed_payloads_fail_open(tmp_path: Path) -> None:
    _seed_file("s1", "/proj/a.py")
    assert checkpoint.handle_session_start([], tmp_path) == {}  # type: ignore[arg-type]
    assert checkpoint.handle_session_start({"source": "clear", "cwd": 3}, tmp_path) == {}
    assert checkpoint.handle_session_start({"source": "clear"}, tmp_path) == {}
    assert (
        "no live session" in checkpoint.handle_checkpoint_save({"cwd": None}, tmp_path)["summary"]
    )
    path = checkpoint.checkpoint_path(tmp_path, "/proj")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    assert checkpoint.handle_session_start(_clear_event(), tmp_path) == {}


def _cli(*args: str, state_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cfxd", "checkpoint", *args, "--state-dir", str(state_dir)],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_show_and_clear(tmp_path: Path) -> None:
    _seed_file("s1", "/proj/a.py")
    checkpoint.handle_checkpoint_save({"cwd": "/proj"}, tmp_path)

    shown = _cli("show", "--cwd", "/proj", state_dir=tmp_path)
    assert shown.returncode == 0, shown.stderr
    assert '<context-fabric-carry v="0" trigger="checkpoint">' in shown.stdout
    assert "local only; not sent anywhere except into your next session" in shown.stdout

    cleared = _cli("clear", "--cwd", "/proj", state_dir=tmp_path)
    assert cleared.returncode == 0
    assert "cleared" in cleared.stdout
    assert not checkpoint.checkpoint_path(tmp_path, "/proj").exists()

    again = _cli("clear", "--cwd", "/proj", state_dir=tmp_path)
    assert "no checkpoint to clear" in again.stdout
    missing = _cli("show", "--cwd", "/proj", state_dir=tmp_path)
    assert missing.returncode == 1
    assert "no checkpoint" in missing.stderr


def _ts(offset: float = 0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset)).isoformat()


def _tool_use(tid: str, name: str, tool_input: dict[str, Any], ts: str) -> dict[str, Any]:
    block = {"type": "tool_use", "id": tid, "name": name, "input": tool_input}
    return {"type": "assistant", "timestamp": ts, "message": {"content": [block]}}


def _tool_result(tid: str, content: str, ts: str, is_error: bool | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error is not None:
        block["is_error"] = is_error
    return {"type": "user", "timestamp": ts, "message": {"content": [block]}}


def _write_transcript(
    config: Path,
    cwd: str,
    session_id: str,
    objects: list[dict[str, Any]],
    *,
    mtime: float | None = None,
) -> Path:
    project = config / "projects" / checkpoint._project_dir_name(cwd)
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(o) for o in objects) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _save_receipt(state_dir: Path) -> dict[str, Any]:
    entries = [
        json.loads(line)
        for line in (state_dir / checkpoint.CHECKPOINT_LOG).read_text(encoding="utf-8").splitlines()
    ]
    return [e for e in entries if e["event"] == "checkpoint_save"][-1]


def _obj(state_dir: Path, cwd: str = "/proj") -> dict[str, Any]:
    return json.loads(checkpoint.checkpoint_path(state_dir, cwd).read_text(encoding="utf-8"))


def test_save_uses_transcript_when_daemon_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    ts = _ts()
    _write_transcript(
        config,
        "/proj",
        "sess-t",
        [
            _tool_use("t1", "Edit", {"file_path": "/proj/a.py"}, ts),
            _tool_use("t2", "Bash", {"command": "uv run pytest -q"}, ts),
            _tool_result("t2", "Exit code 1", ts, is_error=True),
            _tool_use(
                "t3", "TodoWrite", {"todos": [{"content": "ship it", "status": "pending"}]}, ts
            ),
        ],
    )
    state_dir = tmp_path / "cfx"
    result = checkpoint.handle_checkpoint_save({"cwd": "/proj"}, state_dir)
    assert result["summary"] == "Checkpoint saved: 1 files, 1 commands, 1 todos"
    obj = _obj(state_dir)
    assert obj["sid"] == "sess-t"
    assert len(obj["files"]) == 1
    assert obj["files"][0]["edited"] is True
    assert obj["files"][0]["lines"] is None
    assert obj["commands"] == ["uv run pytest -q"]
    assert obj["todos"] == [{"s": "pending", "t": "ship it"}]
    assert obj["test"]["key"] == "pytest"
    assert obj["test"]["ok"] is False
    receipt = _save_receipt(state_dir)
    assert receipt["source"] == "transcript"
    assert (receipt["daemon_files"], receipt["transcript_files"], receipt["merged_files"]) == (
        0,
        1,
        1,
    )


def test_merge_keeps_daemon_facts_and_adds_transcript_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    _seed_file("s1", "/proj/a.py", edited=True, lines=10)
    ts = _ts()
    _write_transcript(
        config,
        "/proj",
        "s1",
        [
            _tool_use("t1", "Read", {"file_path": "/proj/a.py"}, ts),
            _tool_use("t2", "Write", {"file_path": "/proj/b.py"}, ts),
        ],
    )
    state_dir = tmp_path / "cfx"
    checkpoint.handle_checkpoint_save({"cwd": "/proj"}, state_dir)
    obj = _obj(state_dir)
    by_path = {Path(f["path"]).name: f for f in obj["files"]}
    assert by_path["a.py"]["edited"] is True
    assert by_path["a.py"]["lines"] == 10
    assert by_path["b.py"]["edited"] is True
    receipt = _save_receipt(state_dir)
    assert receipt["source"] == "merged"
    assert (receipt["daemon_files"], receipt["transcript_files"], receipt["merged_files"]) == (
        1,
        2,
        2,
    )


def test_daemon_full_reports_daemon_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    _seed_file("s1", "/proj/a.py", lines=10)
    _write_transcript(
        config, "/proj", "s1", [_tool_use("t1", "Read", {"file_path": "/proj/a.py"}, _ts())]
    )
    state_dir = tmp_path / "cfx"
    checkpoint.handle_checkpoint_save({"cwd": "/proj"}, state_dir)
    receipt = _save_receipt(state_dir)
    assert receipt["source"] == "daemon"
    assert (receipt["daemon_files"], receipt["transcript_files"], receipt["merged_files"]) == (
        1,
        1,
        1,
    )
    assert _obj(state_dir)["files"][0]["lines"] == 10


def test_newest_transcript_within_six_hours_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    now = datetime.now(UTC).timestamp()
    _write_transcript(
        config,
        "/proj",
        "old",
        [_tool_use("t1", "Read", {"file_path": "/proj/old.py"}, _ts())],
        mtime=now - 7 * 3600,
    )
    _write_transcript(
        config,
        "/proj",
        "new",
        [_tool_use("t1", "Read", {"file_path": "/proj/new.py"}, _ts())],
        mtime=now - 3600,
    )
    state_dir = tmp_path / "cfx"
    checkpoint.handle_checkpoint_save({"cwd": "/proj"}, state_dir)
    assert _obj(state_dir)["sid"] == "new"

    stale = tmp_path / "stale"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(stale))
    _write_transcript(
        stale,
        "/only-old",
        "old",
        [_tool_use("t1", "Read", {"file_path": "/only-old/a.py"}, _ts())],
        mtime=now - 7 * 3600,
    )
    result = checkpoint.handle_checkpoint_save({"cwd": "/only-old"}, tmp_path / "cfx2")
    assert "no live session" in result["summary"]
    assert not checkpoint.checkpoint_path(tmp_path / "cfx2", "/only-old").exists()


def test_large_transcript_reads_only_the_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    ts = _ts()
    filler = {"type": "user", "timestamp": ts, "message": {"content": "x" * (5 * 1024 * 1024)}}
    path = _write_transcript(
        config,
        "/proj",
        "s1",
        [
            _tool_use("t0", "Read", {"file_path": "/proj/old.py"}, ts),
            filler,
            _tool_use("t9", "Read", {"file_path": "/proj/new.py"}, ts),
        ],
    )
    size = path.stat().st_size
    assert size > checkpoint.TRANSCRIPT_TAIL_BYTES

    read_bytes = 0
    real_open = builtins.open

    def counting_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        handle = real_open(file, *args, **kwargs)
        if Path(file) != path:
            return handle

        class Wrapper:
            def __getattr__(self, name: str) -> Any:
                return getattr(handle, name)

            def read(self, *a: Any, **k: Any) -> bytes:
                nonlocal read_bytes
                data = handle.read(*a, **k)
                read_bytes += len(data)
                return data

            def __enter__(self) -> Wrapper:
                return self

            def __exit__(self, *a: Any) -> Any:
                return handle.__exit__(*a)

        return Wrapper()

    monkeypatch.setattr(builtins, "open", counting_open)
    state_dir = tmp_path / "cfx"
    checkpoint.handle_checkpoint_save({"cwd": "/proj"}, state_dir)
    assert read_bytes <= checkpoint.TRANSCRIPT_TAIL_BYTES
    assert read_bytes < size
    names = {Path(f["path"]).name for f in _obj(state_dir)["files"]}
    assert names == {"new.py"}


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_tool_result("t1", "Exit code 2", _ts(), is_error=True), False),
        (_tool_result("t1", "Exit code 0", _ts(), is_error=False), True),
        (_tool_result("t1", "collected 3 items", _ts()), True),
        (None, None),
    ],
)
def test_test_run_status_from_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: dict[str, Any] | None,
    expected: bool | None,
) -> None:
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    objects = [_tool_use("t1", "Bash", {"command": "cd x && uv run pytest 2>&1"}, _ts())]
    if result is not None:
        objects.append(result)
    _write_transcript(config, "/proj", "s1", objects)
    state_dir = tmp_path / "cfx"
    checkpoint.handle_checkpoint_save({"cwd": "/proj"}, state_dir)
    assert _obj(state_dir)["test"]["key"] == "pytest"
    assert _obj(state_dir)["test"]["ok"] is expected


def test_transcript_commands_are_masked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    secret = "ghp_SECRETCANARY1234567890"
    _write_transcript(
        config,
        "/proj",
        "s1",
        [_tool_use("t1", "Bash", {"command": f"export API_TOKEN={secret}"}, _ts())],
    )
    state_dir = tmp_path / "cfx"
    checkpoint.handle_checkpoint_save({"cwd": "/proj"}, state_dir)
    text = checkpoint.checkpoint_path(state_dir, "/proj").read_text(encoding="utf-8")
    assert secret not in text
    assert "API_TOKEN=***" in text


def test_transcript_text_never_reaches_the_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    canary = "CANARY-TRANSCRIPT-44444444"
    ts = _ts()
    _write_transcript(
        config,
        "/proj",
        "s1",
        [
            {"type": "user", "timestamp": ts, "message": {"content": f"prompt {canary}"}},
            _tool_use("t1", "Read", {"file_path": "/proj/a.py"}, ts),
            {"type": "assistant", "timestamp": ts, "message": {"content": f"text {canary}"}},
            _tool_result("t1", f"contents {canary}", ts),
        ],
    )
    state_dir = tmp_path / "cfx"
    checkpoint.handle_checkpoint_save({"cwd": "/proj"}, state_dir)
    packet = checkpoint.checkpoint_path(state_dir, "/proj").read_text(encoding="utf-8")
    receipt = (state_dir / checkpoint.CHECKPOINT_LOG).read_text(encoding="utf-8")
    assert canary not in packet
    assert canary not in receipt


def test_stats_reports_checkpoint_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cfxd.stats import collect_stats, format_human

    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    _write_transcript(
        config,
        "/proj",
        "s1",
        [_tool_use("t1", "Read", {"file_path": "/proj/a.py"}, _ts())],
    )
    state_dir = tmp_path / "cfx"
    checkpoint.handle_checkpoint_save({"cwd": "/proj"}, state_dir)
    stats = collect_stats(state_dir)
    assert stats["checkpoints"] == {
        "saves": 1,
        "daemon_only": 0,
        "transcript_added": 0,
        "transcript_only": 1,
    }
    line = "checkpoint saves: 1 (daemon only 0, transcript added 0, transcript only 1)"
    assert line in format_human(stats)


def _post(port: int, event: str, payload: bytes, token: str) -> tuple[int, dict[str, Any]]:
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(
            "POST",
            f"/hooks/{event}",
            body=payload,
            headers={"Content-Type": "application/json", "X-Cfx-Token": token},
        )
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, json.loads(body) if body else {}
    finally:
        conn.close()


def test_end_to_end_save_then_clear_start(tmp_path: Path) -> None:
    d = Daemon(tmp_path / "cfx", 0)
    d.start()
    try:
        read_payload = json.dumps(
            {
                "tool_name": "Read",
                "session_id": "sess-1",
                "cwd": "/proj",
                "tool_input": {"file_path": "/proj/a.py"},
                "tool_response": {"file": {"numLines": 9, "totalLines": 9}},
            }
        ).encode("utf-8")
        assert _post(d.port, "PostToolUse", read_payload, d.token)[0] == 200
        status, body = _post(
            d.port, "checkpoint-save", json.dumps({"cwd": "/proj"}).encode("utf-8"), d.token
        )
        assert status == 200
        assert body == {"summary": "Checkpoint saved: 1 files, 0 commands, 0 todos"}
        status, body = _post(
            d.port,
            "SessionStart",
            json.dumps({"session_id": "sess-1", "source": "clear", "cwd": "/proj"}).encode("utf-8"),
            d.token,
        )
        assert status == 200
        assert 'trigger="checkpoint"' in body["hookSpecificOutput"]["additionalContext"]
    finally:
        d.stop()
        clear()
