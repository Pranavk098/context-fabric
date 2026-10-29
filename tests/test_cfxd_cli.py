"""cfxd CLI tests: stop, status, and doctor against a subprocess daemon."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run_cli(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cfxd", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _wait_for(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert path.exists(), f"{path} was not created in time"


def test_stop_removes_state_file(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    state_file = state_dir / "state.json"
    proc = subprocess.Popen(
        [sys.executable, "-m", "cfxd", "serve", "--state-dir", str(state_dir), "--port", "0"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(state_file)
        result = _run_cli("stop", "--state-dir", str(state_dir))
        assert result.returncode == 0, result.stderr
        assert not state_file.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def test_status_not_running(tmp_path: Path) -> None:
    result = _run_cli("status", "--state-dir", str(tmp_path / "cfx"))
    assert result.returncode == 1
    assert "not running" in result.stdout


def test_doctor_no_state_exits_1(tmp_path: Path) -> None:
    result = _run_cli("doctor", "--state-dir", str(tmp_path / "cfx"))
    assert result.returncode == 1
    assert "no state file" in result.stdout


def test_serve_port_in_use_exits_2(tmp_path: Path) -> None:
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        result = _run_cli("serve", "--state-dir", str(tmp_path / "cfx"), "--port", str(port))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "in use" in result.stderr


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


def _fake_health_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_stop_does_not_kill_foreign_process(tmp_path: Path) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    server = _fake_health_server()
    _HealthHandler.pid = 1  # a /health that claims a different pid than the state
    try:
        state_dir = tmp_path / "cfx"
        state_dir.mkdir(parents=True)
        (state_dir / "state.json").write_text(
            json.dumps({"port": server.server_address[1], "pid": child.pid}),
            encoding="utf-8",
        )
        result = _run_cli("stop", "--state-dir", str(state_dir))
        time.sleep(0.3)
        assert child.poll() is None, "stop killed a process it does not own"
        assert not (state_dir / "state.json").exists()
        assert "stale state" in result.stdout
    finally:
        server.shutdown()
        server.server_close()
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_doctor_sh_check_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from cfxd import cli

    name = "sh.cmd" if os.name == "nt" else "sh"
    fake = tmp_path / name
    fake.write_text("@echo ok\n" if os.name == "nt" else "#!/bin/sh\necho ok\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    ok, line = cli._sh_check()
    assert ok
    assert "found" in line
    assert name.lower() in line.lower()


def test_doctor_sh_check_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from cfxd import cli

    monkeypatch.setenv("PATH", str(tmp_path))
    ok, line = cli._sh_check()
    assert not ok
    assert "https://git-scm.com/download/win" in line
    assert 'sh -c "echo ok"' in line


def test_doctor_prints_identity_verified(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    state_file = state_dir / "state.json"
    proc = subprocess.Popen(
        [sys.executable, "-m", "cfxd", "serve", "--state-dir", str(state_dir), "--port", "0"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(state_file)
        result = _run_cli("doctor", "--state-dir", str(state_dir), timeout=120)
        assert "identity:" in result.stdout
        assert "verified" in result.stdout
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
