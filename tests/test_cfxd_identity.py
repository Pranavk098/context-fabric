"""cfxd daemon-identity tests (finding 1 mitigation).

The daemon mints a 0600 token file and returns an HMAC proof for a nonce; the
launcher verifies that proof before forwarding SessionStart. A fake server that
answers /health but cannot prove identity must trigger the loud warning.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cfxd.daemon import Daemon, verify_identity

ROOT = Path(__file__).resolve().parents[1]
LAUNCH = ROOT / "cfxd" / "launch.py"

_WARNING = (
    '{"systemMessage":"Context Fabric: something else is using port 47831, '
    "or an old Context Fabric daemon is running. Hooks are not protected. "
    'Restart your computer or run: python -m cfxd stop"}'
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _FakeHandler(BaseHTTPRequestHandler):
    proof: str | None = None
    post_count = 0

    def do_GET(self) -> None:
        health: dict[str, object] = {"ok": True}
        if self.proof is not None:
            health["proof"] = self.proof
        body = json.dumps(health).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        type(self).post_count += 1
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def fake_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _run_launch(args: list[str], state_dir: Path, port: int) -> str:
    env = {**os.environ, "CFX_HOME": str(state_dir), "CFX_PORT": str(port)}
    proc = subprocess.run(
        [sys.executable, str(LAUNCH), *args],
        input=b"{}",
        capture_output=True,
        env=env,
        cwd=str(ROOT),
        timeout=30,
    )
    return proc.stdout.decode("utf-8")


def test_honest_daemon_verifies(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    d = Daemon(state_dir, 0)
    d.start()
    try:
        assert verify_identity(state_dir, d.port) is True
    finally:
        d.stop()


def test_fake_server_without_proof_warns(tmp_path: Path, fake_server: ThreadingHTTPServer) -> None:
    state_dir = tmp_path / "cfx"
    state_dir.mkdir(parents=True)
    (state_dir / "token").write_text("a" * 64, encoding="utf-8")
    out = _run_launch(["session-start"], state_dir, fake_server.server_address[1])
    assert out.strip() == _WARNING


def test_fake_server_with_wrong_proof_warns(
    tmp_path: Path, fake_server: ThreadingHTTPServer
) -> None:
    _FakeHandler.proof = "deadbeef"
    try:
        state_dir = tmp_path / "cfx"
        state_dir.mkdir(parents=True)
        (state_dir / "token").write_text("a" * 64, encoding="utf-8")
        out = _run_launch(["session-start"], state_dir, fake_server.server_address[1])
    finally:
        _FakeHandler.proof = None
    assert out.strip() == _WARNING


def test_squatter_receives_no_hook_payload(
    tmp_path: Path, fake_server: ThreadingHTTPServer
) -> None:
    # A server that answers /health but cannot prove identity must receive no
    # hook payload: launch.py forward verifies before sending anything.
    state_dir = tmp_path / "cfx"
    state_dir.mkdir(parents=True)
    (state_dir / "token").write_text("a" * 64, encoding="utf-8")
    _FakeHandler.post_count = 0
    out = _run_launch(["forward", "PostToolUse"], state_dir, fake_server.server_address[1])
    assert out.strip() == "{}"
    assert _FakeHandler.post_count == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_token_file_perms_0600(tmp_path: Path) -> None:
    d = Daemon(tmp_path / "cfx", 0)
    d.start()
    try:
        mode = stat.S_IMODE((tmp_path / "cfx" / "token").stat().st_mode)
        assert mode == 0o600
    finally:
        d.stop()
