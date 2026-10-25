"""cfxd.hookclient tests: token read, identity proof, and authenticated POST.

A fake HTTP server models the daemon: it answers ``/health`` with an HMAC proof
for the nonce and enforces the ``X-Cfx-Token`` header on ``POST /hooks/*``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cfxd.hookclient import post_hook, read_token, verify_identity

TOKEN = "a" * 64


class _FakeDaemonHandler(BaseHTTPRequestHandler):
    token = TOKEN
    wrong_proof = False

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        health: dict[str, object] = {"ok": True}
        if "nonce=" in self.path:
            nonce = self.path.rsplit("nonce=", 1)[1]
            proof = hmac.new(self.token.encode(), nonce.encode(), hashlib.sha256).hexdigest()
            health["proof"] = "0" * 64 if self.wrong_proof else proof
        self._send(200, json.dumps(health).encode())

    def do_POST(self) -> None:
        supplied = self.headers.get("X-Cfx-Token") or ""
        if self.token and hmac.compare_digest(supplied, self.token):
            self._send(200, b"{}")
        else:
            self._send(401, json.dumps({"error": "unauthorized"}).encode())

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[ThreadingHTTPServer]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeDaemonHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def _port(httpd: ThreadingHTTPServer) -> int:
    return httpd.server_address[1]


def _write_token(state_dir: Path, token: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "token").write_text(token, encoding="utf-8")


def test_read_token_missing_returns_none(tmp_path: Path) -> None:
    assert read_token(tmp_path) is None


def test_read_token_returns_value(tmp_path: Path) -> None:
    _write_token(tmp_path, TOKEN)
    assert read_token(tmp_path) == TOKEN


def test_verify_identity_missing_token_fails(tmp_path: Path, server: ThreadingHTTPServer) -> None:
    assert verify_identity(tmp_path, _port(server)) is False


def test_verify_identity_wrong_proof_fails(tmp_path: Path, server: ThreadingHTTPServer) -> None:
    _write_token(tmp_path, TOKEN)
    _FakeDaemonHandler.wrong_proof = True
    try:
        assert verify_identity(tmp_path, _port(server)) is False
    finally:
        _FakeDaemonHandler.wrong_proof = False


def test_verify_identity_correct_proof_passes(tmp_path: Path, server: ThreadingHTTPServer) -> None:
    _write_token(tmp_path, TOKEN)
    assert verify_identity(tmp_path, _port(server)) is True


def test_post_hook_wrong_token_401(server: ThreadingHTTPServer) -> None:
    status, _ = post_hook(_port(server), "PostToolUse", b"{}", token="wrong")
    assert status == 401


def test_post_hook_correct_token_200(server: ThreadingHTTPServer) -> None:
    status, _ = post_hook(_port(server), "PostToolUse", b"{}", token=TOKEN)
    assert status == 200
