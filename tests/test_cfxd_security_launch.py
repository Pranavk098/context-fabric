"""Security fixes B: launcher identity/note-file, stop identity, diet argv, daemon rejects.

Each test calls the real function; the daemon runs in-process like test_cfxd_daemon.py.
"""

from __future__ import annotations

import argparse
import http.client
import io
import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

import cfxd.agents.opencode as opencode
import cfxd.cli as cli
import cfxd.dietapply as dietapply
from cfxd import checkpoint, launch
from cfxd import session_state as ss
from cfxd.daemon import Daemon
from cfxd.handlers import clear


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    ss.reset()
    checkpoint.reset()
    clear()
    yield
    ss.reset()
    checkpoint.reset()
    clear()


class _Stdin:
    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


# --------------------------------------------------------------------- F5


def test_session_start_fall_through_never_sends_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "cfx"
    state_dir.mkdir()
    (state_dir / "token").write_text("tok", encoding="utf-8")
    monkeypatch.setenv("CFX_HOME", str(state_dir))
    monkeypatch.setattr(launch, "_health_ok", lambda port: False)
    monkeypatch.setattr(launch, "_ensure_daemon", lambda port: False)
    monkeypatch.setattr(launch, "verify_identity", lambda *a: False)
    monkeypatch.setattr(launch.sys, "stdin", _Stdin(b'{"session_id": "s1"}'))
    tokens: list[str | None] = []

    def fake_post_hook(
        port: int, event: str, payload: bytes, timeout: float = 1.0, token: str | None = None
    ) -> tuple[int, bytes]:
        tokens.append(token)
        return 404, b"{}"

    monkeypatch.setattr(launch, "post_hook", fake_post_hook)
    assert launch._session_start() == 0
    assert tokens == [None]


# --------------------------------------------------------------------- F4


def test_note_file_outside_plugin_data_is_ignored_and_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "plugin-data"
    data.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data))
    victim = tmp_path / "victim.txt"
    victim.write_text("secret", encoding="utf-8")
    assert launch._checkpoint_note(["--note-file", str(victim)]) is None
    assert victim.read_text(encoding="utf-8") == "secret"


def test_note_file_wrong_name_in_plugin_data_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "plugin-data"
    data.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data))
    victim = data / "victim.txt"
    victim.write_text("secret", encoding="utf-8")
    assert launch._checkpoint_note(["--note-file", str(victim)]) is None
    assert victim.exists()
    # A traversal out of the data dir is rejected even when the name looks right.
    outside = tmp_path / "checkpoint-note-evil.txt"
    outside.write_text("secret", encoding="utf-8")
    assert launch._checkpoint_note(["--note-file", str(data / ".." / outside.name)]) is None
    assert outside.exists()


def test_valid_note_file_is_read_and_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "plugin-data"
    data.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data))
    note = data / "checkpoint-note-abc-123.txt"
    note.write_text("some goal", encoding="utf-8")
    assert launch._checkpoint_note(["--note-file", str(note)]) == "some goal"
    assert not note.exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_note_file_symlink_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "plugin-data"
    data.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data))
    target = data / "checkpoint-note-real.txt"
    target.write_text("secret", encoding="utf-8")
    link = data / "checkpoint-note-link.txt"
    link.symlink_to(target)
    assert launch._checkpoint_note(["--note-file", str(link)]) is None
    assert target.exists() and link.exists()


def test_checkpoint_slash_command_still_saves_a_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = Daemon(tmp_path / "cfx", 0)
    daemon.start()
    try:
        monkeypatch.setenv("CFX_HOME", str(daemon.state_dir))
        monkeypatch.setenv("CFX_PORT", str(daemon.port))
        data = tmp_path / "plugin-data"
        data.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data))
        note = data / "checkpoint-note-session-1.txt"
        note.write_text("some goal", encoding="utf-8")
        cwd = os.getcwd()
        file_path = tmp_path / "a.py"
        file_path.write_text("x", encoding="utf-8")
        checkpoint.observe_cwd({"session_id": "s1", "cwd": cwd})
        ss.observe_post_tool_use(
            {
                "session_id": "s1",
                "tool_name": "Read",
                "tool_input": {"file_path": str(file_path)},
                "tool_response": {"file": {"numLines": 1, "totalLines": 1}},
            }
        )
        assert launch._checkpoint_save(["--note-file", str(note)]) == 0
        assert not note.exists()
        saved = json.loads(checkpoint.checkpoint_path(daemon.state_dir, cwd).read_text("utf-8"))
        assert saved["note"] == "some goal"
    finally:
        daemon.stop()


# --------------------------------------------------------------------- F6


class _HealthHandler(BaseHTTPRequestHandler):
    pid = 0

    def do_GET(self) -> None:
        body = json.dumps({"ok": True, "pid": self.pid}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def test_stop_requires_identity_before_killing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    state_dir = tmp_path / "cfx"
    state_dir.mkdir()
    (state_dir / "token").write_text("tok", encoding="utf-8")
    port = server.server_address[1]
    _HealthHandler.pid = 424242
    (state_dir / "state.json").write_text(
        json.dumps({"port": port, "pid": 424242}), encoding="utf-8"
    )
    killed: list[int] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
    try:
        assert cli._cmd_stop(argparse.Namespace(state_dir=str(state_dir))) == 0
    finally:
        server.shutdown()
        server.server_close()
    assert killed == []
    out = capsys.readouterr().out
    assert "stale state" in out
    assert not (state_dir / "state.json").exists()
    assert (state_dir / "token").exists()


# -------------------------------------------------------------------- F10


def _report(*names: str) -> dict[str, Any]:
    return {
        "sessions": 1,
        "never_used": {
            "plugins": [{"plugin": name, "count": 1, "per_turn_tokens": 1.0} for name in names],
            "connectors": [],
        },
        "used": {"plugins": [], "connectors": []},
    }


def test_apply_never_passes_invalid_plugin_ids_to_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CFX_HOME", str(tmp_path / "cfx"))
    plugins = [
        {"id": "--x@m", "scope": "user", "enabled": True},
        {"id": "a&b@m", "scope": "user", "enabled": True},
    ]
    calls: list[list[str]] = []
    monkeypatch.setattr(dietapply, "_claude", lambda: "claude")
    monkeypatch.setattr(dietapply, "_list_plugins", lambda exe: plugins)
    monkeypatch.setattr(dietapply, "_run", lambda exe, args: (calls.append(args), (0, ""))[1])
    assert dietapply.apply_report(_report("--x", "a&b"), yes=True) == 0
    assert calls == []
    assert "not applied (invalid id)" in capsys.readouterr().out


# -------------------------------------------------------------------- F11


def _send_raw(port: int, path: str, headers: dict[str, str], *, body: bytes = b"") -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("POST", path, skip_host=True)
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders()
        if body:
            conn.send(body)
        resp = conn.getresponse()
        resp.read()
        return int(resp.status)
    finally:
        conn.close()


def test_bad_host_reject_is_not_logged_and_reject_event_is_truncated(tmp_path: Path) -> None:
    daemon = Daemon(tmp_path / "cfx", 0)
    daemon.start()
    long_event = "A" * 60000
    try:
        host = f"127.0.0.1:{daemon.port}"
        assert (
            _send_raw(
                daemon.port,
                f"/hooks/{long_event}",
                {"Host": "evil.example.com", "Content-Type": "application/json"},
            )
            == 403
        )
        assert not daemon.events_path.exists()

        # A non-Host reject still logs, with the attacker-chosen name capped at 64 chars.
        assert (
            _send_raw(
                daemon.port,
                f"/hooks/{long_event}",
                {
                    "Host": host,
                    "Origin": "https://evil.example.com",
                    "Content-Type": "application/json",
                },
            )
            == 403
        )
        lines = daemon.events_path.read_text(encoding="utf-8").splitlines()
        last = json.loads(lines[-1])
        assert last["outcome"] == "rejected"
        assert len(last["event"]) <= 64
        assert last["event"] == "A" * 64
    finally:
        daemon.stop()


# -------------------------------------------------------------------- F12


def test_doctor_warns_when_state_dir_is_outside_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    outside = tmp_path / "elsewhere"
    monkeypatch.setattr(cli, "_IS_WINDOWS", True)
    monkeypatch.setenv("USERPROFILE", str(profile))
    assert cli._cmd_doctor(argparse.Namespace(state_dir=str(outside))) == 1
    out = capsys.readouterr().out
    assert out.count("warning: state directory is outside your user profile") == 1


def test_doctor_is_quiet_inside_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = tmp_path / "profile"
    (profile / "cfx").mkdir(parents=True)
    monkeypatch.setattr(cli, "_IS_WINDOWS", True)
    monkeypatch.setenv("USERPROFILE", str(profile))
    assert cli._cmd_doctor(argparse.Namespace(state_dir=str(profile / "cfx"))) == 1
    assert "outside your user profile" not in capsys.readouterr().out


# -------------------------------------------------------------------- F14


class _FakeConn:
    def execute(self, *args: Any, **kwargs: Any) -> None:
        return None

    def close(self) -> None:
        return None


def test_opencode_uri_percent_encodes_query_and_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "we?ird#name.db"
    monkeypatch.setattr(opencode.Path, "exists", lambda self: True)
    captured: dict[str, str] = {}

    def fake_connect(database: str, **kwargs: Any) -> _FakeConn:
        captured["uri"] = database
        return _FakeConn()

    monkeypatch.setattr(opencode.sqlite3, "connect", fake_connect)
    opencode._connect(db).close()
    assert captured["uri"] == db.resolve().as_uri() + "?mode=ro"
    assert "%3F" in captured["uri"] and "%23" in captured["uri"]


@pytest.mark.skipif(os.name == "nt", reason="Windows filenames cannot contain ? or #")
def test_opencode_opens_real_db_with_special_chars(tmp_path: Path) -> None:
    folder = tmp_path / "we?ird#dir"
    folder.mkdir()
    db = folder / "opencode.db"
    sqlite3.connect(db).close()
    con = opencode._connect(db)
    try:
        assert con.execute("SELECT 1").fetchone() == (1,)
    finally:
        con.close()
