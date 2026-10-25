"""cfxd launch.py tests: the launcher starts, reuses, and forwards to the daemon.

The launcher is exercised as a subprocess from a cwd other than the repo root to
prove it works from anywhere. Daemons it starts are stopped in fixture teardown.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCH = ROOT / "cfxd" / "launch.py"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _env(state_dir: Path, port: int) -> dict[str, str]:
    return {**os.environ, "CFX_HOME": str(state_dir), "CFX_PORT": str(port)}


def _run_launch(args: list[str], payload: bytes, env: dict[str, str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(LAUNCH), *args],
        input=payload,
        capture_output=True,
        env=env,
        cwd=str(cwd),
        timeout=30,
    )
    return proc.returncode, proc.stdout.decode("utf-8")


def _stop_daemon(state_dir: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "cfxd", "stop"],
        cwd=str(ROOT),
        env={**os.environ, "CFX_HOME": str(state_dir)},
        capture_output=True,
        timeout=30,
    )


def _health_ok(port: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
            conn.request("GET", "/health")
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            if resp.status == 200 and json.loads(body).get("ok") is True:
                return True
        except OSError:
            pass
        time.sleep(0.05)
    return False


def _read_events(state_dir: Path) -> list[dict[str, object]]:
    path = state_dir / "events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


@pytest.fixture
def state(tmp_path: Path) -> Iterator[tuple[int, Path]]:
    port = _free_port()
    state_dir = tmp_path / "cfx"
    yield port, state_dir
    _stop_daemon(state_dir)


def test_ensure_starts_daemon(state: tuple[int, Path], tmp_path: Path) -> None:
    port, state_dir = state
    rc, out = _run_launch(["ensure"], b"", _env(state_dir, port), tmp_path)
    assert rc == 0
    assert "started" in out
    assert _health_ok(port)
    written = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert written["port"] == port
    assert isinstance(written["pid"], int)


def test_ensure_is_idempotent(state: tuple[int, Path], tmp_path: Path) -> None:
    port, state_dir = state
    _run_launch(["ensure"], b"", _env(state_dir, port), tmp_path)
    pid1 = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))["pid"]
    rc, out = _run_launch(["ensure"], b"", _env(state_dir, port), tmp_path)
    pid2 = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))["pid"]
    assert rc == 0
    assert "already running" in out
    assert pid1 == pid2
    assert _health_ok(port)


def test_session_start_forwards_and_prints_daemon_json(
    state: tuple[int, Path], tmp_path: Path
) -> None:
    port, state_dir = state
    payload = {"session_id": "abc-123", "source": "startup"}
    rc, out = _run_launch(
        ["session-start"], json.dumps(payload).encode(), _env(state_dir, port), tmp_path
    )
    assert rc == 0
    assert json.loads(out) == {}
    events = _read_events(state_dir)
    assert any(e["event"] == "SessionStart" and e["session_id"] == "abc-123" for e in events)


def test_session_start_prints_empty_when_port_occupied(tmp_path: Path) -> None:
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    state_dir = tmp_path / "cfx"
    try:
        rc, out = _run_launch(["session-start"], b"{}", _env(state_dir, port), tmp_path)
    finally:
        blocker.close()
    assert rc == 0
    assert out.strip() == "{}"


def test_forward_without_daemon_prints_empty_and_does_not_spawn(
    state: tuple[int, Path], tmp_path: Path
) -> None:
    port, state_dir = state
    rc, out = _run_launch(["forward", "PostToolUse"], b"{}", _env(state_dir, port), tmp_path)
    assert rc == 0
    assert out.strip() == "{}"
    assert not (state_dir / "state.json").exists()


def test_forward_missing_token_file_sends_nothing(state: tuple[int, Path], tmp_path: Path) -> None:
    port, state_dir = state
    _run_launch(["ensure"], b"", _env(state_dir, port), tmp_path)
    (state_dir / "token").unlink()
    rc, out = _run_launch(["forward", "PostToolUse"], b"{}", _env(state_dir, port), tmp_path)
    assert rc == 0
    assert out.strip() == "{}"
    assert _read_events(state_dir) == []


def test_forward_to_running_daemon_sends_token(state: tuple[int, Path], tmp_path: Path) -> None:
    port, state_dir = state
    _run_launch(["ensure"], b"", _env(state_dir, port), tmp_path)
    payload = {"session_id": "sess-1", "tool_name": "Read"}
    rc, out = _run_launch(
        ["forward", "PostToolUse"], json.dumps(payload).encode(), _env(state_dir, port), tmp_path
    )
    assert rc == 0
    assert out.strip() == "{}"
    assert any(e["event"] == "PostToolUse" for e in _read_events(state_dir))


def test_hooks_json_shape() -> None:
    data = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    events = data["hooks"]
    for event, groups in events.items():
        for group in groups:
            for hook in group["hooks"]:
                assert hook["type"] == "command", f"{event} must be a command hook"
                assert hook["shell"] == "bash"
                assert hook["command"].startswith('sh "${CLAUDE_PLUGIN_ROOT}/cfxd/launch.sh" ')

    session_start = events["SessionStart"][0]["hooks"][0]
    assert session_start["command"].endswith(" session-start")

    pre_compact = events["PreCompact"][0]["hooks"][0]
    assert pre_compact["command"].endswith(" forward PreCompact")

    pre_tool = events["PreToolUse"][0]
    assert pre_tool["matcher"] == "Agent"
    assert pre_tool["hooks"][0]["command"].endswith(" forward PreToolUse")

    post_tool = events["PostToolUse"][0]
    assert post_tool["matcher"] == "*"
    assert post_tool["hooks"][0]["command"].endswith(" forward PostToolUse")

    # SubagentStart/SubagentStop have no handlers: they must be gone.
    assert "SubagentStart" not in events
    assert "SubagentStop" not in events


def test_launcher_never_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cfxd import launch

    def boom(args: list[str]) -> int:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(launch, "_dispatch", boom)
    assert launch.main(["session-start"]) == 0
    assert capsys.readouterr().out.strip() == "{}"
