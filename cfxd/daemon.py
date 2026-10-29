"""Resident Context Fabric daemon: a 127.0.0.1 hook endpoint that fails open.

Any handler error or slow handler returns ``{}`` with HTTP 200, so a hook can
never block the agent. Requests are logged to ``events.jsonl`` (metadata only —
never payload content, which may contain user code).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import socket
import socketserver
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlparse

from cfxd import __version__, session_state
from cfxd.bound import install as bound_install
from cfxd.brief import install as brief_install
from cfxd.carry import install as carry_install
from cfxd.checkpoint import install as checkpoint_install
from cfxd.coach import install as coach_install
from cfxd.fold import install
from cfxd.fsutil import ensure_dir, open_log, rotate
from cfxd.handlers import add_observer, dispatch
from cfxd.hookclient import (  # noqa: F401  # client-side helpers, re-exported for CLI + launcher
    DEFAULT_PORT,
    TOKEN_FILE,
    _health_ok,
    get_health,
    post_hook,
    read_token,
    resolve_state_dir,
    verify_identity,
)
from cfxd.idle import install as idle_install

# Re-exported so `from cfxd.daemon import ...` keeps working under mypy's
# --no-implicit-reexport (part of --strict).
__all__ = [
    "DEFAULT_PORT",
    "get_health",
    "post_hook",
    "read_token",
    "resolve_state_dir",
    "verify_identity",
]

STATE_FILE = "state.json"
EVENTS_FILE = "events.jsonl"
HANDLER_TIMEOUT_MS = 150.0
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_CONNECTIONS = 64
EVENTS_ROTATE_BYTES = 20 * 1024 * 1024
_BUSY_RESPONSE = (
    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
)

# Handlers run here so a slow one is abandoned at the deadline instead of holding the hook.
_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="cfxd-handler")


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolated percentile (``p`` in 0..100)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * p / 100.0
    lo = int(index)
    hi = min(lo + 1, len(ordered) - 1)
    frac = index - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def _atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
            fh.flush()
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_state(state_dir: Path, port: int, pid: int) -> None:
    ensure_dir(state_dir)
    _atomic_write_json(
        state_dir / STATE_FILE,
        {
            "port": port,
            "pid": pid,
            "started_at": datetime.now(UTC).isoformat(),
            "version": __version__,
        },
    )


def read_state(path: Path) -> dict[str, Any] | None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _ensure_token(state_dir: Path) -> str:
    """Create ``<state_dir>/token`` (0600, atomic) once and reuse it. Never raises."""
    path = state_dir / TOKEN_FILE
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    token = secrets.token_hex(32)
    fd, tmp = tempfile.mkstemp(dir=str(state_dir), prefix=".token.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token)
            fh.flush()
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return token
    return token


def daemon_alive(state: dict[str, Any]) -> bool:
    port = state.get("port")
    if not isinstance(port, int):
        return False
    return _health_ok(port)


def _field(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _parse_payload(body: bytes) -> tuple[dict[str, Any] | None, str | None, str | None]:
    try:
        parsed = json.loads(body)
    except ValueError:
        return None, None, None
    if not isinstance(parsed, dict):
        return None, None, None
    return parsed, _field(parsed, "tool_name"), _field(parsed, "session_id")


class Daemon:
    def __init__(self, state_dir: Path, port: int) -> None:
        self.state_dir = state_dir
        self.events_path = state_dir / EVENTS_FILE
        self._port = port
        self.port = 0
        self.token = ""
        self._httpd: _CfxdServer | None = None
        self._thread: threading.Thread | None = None
        self._log_lock = threading.Lock()

    def start(self) -> None:
        ensure_dir(self.state_dir)
        self.token = _ensure_token(self.state_dir)
        install(self.state_dir)
        brief_install(self.state_dir)
        bound_install(self.state_dir)
        carry_install(self.state_dir)
        checkpoint_install(self.state_dir)
        coach_install(self.state_dir)
        idle_install(self.state_dir)
        add_observer("PostToolUse", session_state.observe_post_tool_use)
        httpd = _CfxdServer(("127.0.0.1", self._port), _Handler)
        httpd.daemon = self
        self._httpd = httpd
        self.port = httpd.server_address[1]
        write_state(self.state_dir, self.port, os.getpid())
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()

    def wait(self) -> None:
        if self._thread is not None:
            self._thread.join()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        (self.state_dir / STATE_FILE).unlink(missing_ok=True)

    def log_event(self, entry: dict[str, Any]) -> None:
        with self._log_lock:
            rotate(self.events_path, EVENTS_ROTATE_BYTES)
            with open_log(self.events_path) as fh:
                fh.write(json.dumps(entry) + "\n")


class _CfxdServer(ThreadingHTTPServer):
    daemon: Daemon

    if os.name == "nt":
        # On Windows SO_REUSEADDR lets a second server bind the same port, so two
        # sessions starting together could run two daemons. Bind exclusively instead.
        allow_reuse_address = False

    def __init__(self, address: Any, handler: Any) -> None:
        super().__init__(address, handler)
        self._conn_lock = threading.Lock()
        self._in_flight = 0

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        with self._conn_lock:
            self._in_flight += 1
            over = self._in_flight > MAX_CONNECTIONS
        if over:
            try:
                request.sendall(_BUSY_RESPONSE)
                # Drain the unread request so close() sends a clean FIN, not a
                # Windows RST that would hide the 503 from the client.
                request.settimeout(0.1)
                try:
                    while request.recv(4096):
                        pass
                except OSError:
                    pass
            finally:
                self.shutdown_request(request)
                with self._conn_lock:
                    self._in_flight -= 1
            return
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._conn_lock:
                self._in_flight -= 1

    def server_bind(self) -> None:
        if os.name == "nt":
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        # Skip HTTPServer.server_bind: its socket.getfqdn reverse-DNS lookup can
        # stall for seconds on macOS, longer than the launcher waits for health.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"cfxd/{__version__}"
    timeout = 5

    @property
    def _daemon(self) -> Daemon:
        return cast(_CfxdServer, self.server).daemon

    def log_message(self, format: str, *args: Any) -> None:
        # Requests are logged to events.jsonl; keep the default stderr log quiet.
        pass

    def _send_json(self, status: int, obj: dict[str, Any]) -> None:
        self._send_bytes(status, json.dumps(obj).encode("utf-8"))

    def _send_bytes(self, status: int, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self) -> bool:
        host = self.headers.get("Host")
        port = self._daemon.port
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _json_content_type(self) -> bool:
        value = self.headers.get("Content-Type")
        if value is None:
            return False
        return value.split(";", 1)[0].strip().lower() == "application/json"

    def _reject_reason(self) -> str | None:
        # Browsers are the realistic remote attacker against a loopback service;
        # these three checks stop them (DNS-rebinding, CSRF, and form posts).
        if self.headers.get("Origin") is not None:
            return "origin"
        if not self._host_ok():
            return "host"
        if not self._json_content_type():
            return "content-type"
        return None

    def _token_ok(self) -> bool:
        supplied = self.headers.get("X-Cfx-Token")
        token = self._daemon.token
        return isinstance(supplied, str) and bool(token) and hmac.compare_digest(supplied, token)

    def _reject(
        self,
        event: str,
        reason: str,
        start: float,
        bytes_in: int,
        status: int = 403,
        message: str = "forbidden",
    ) -> None:
        # A bad Host means the request was not addressed to our loopback service;
        # nothing about it is worth keeping. The event name is attacker-chosen, so cap it.
        if self._host_ok():
            self._daemon.log_event(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "event": event[:64],
                    "tool_name": None,
                    "session_id": None,
                    "latency_ms": round((time.perf_counter() - start) * 1000.0, 3),
                    "outcome": "rejected",
                    "reason": reason,
                    "bytes_in": bytes_in,
                    "bytes_out": 0,
                }
            )
        self.close_connection = True
        self._send_json(status, {"error": message})

    def _send_close(self, status: int, obj: dict[str, Any]) -> None:
        self.close_connection = True
        self._send_json(status, obj)

    def _content_length(self) -> int | None:
        """Content-Length as an int, ``0`` when absent, ``None`` when malformed/negative."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        try:
            value = int(raw)
        except ValueError:
            return None
        if value < 0:
            return None
        return value

    def do_GET(self) -> None:
        if not self._host_ok():
            self._send_json(403, {"error": "forbidden"})
            return
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            health: dict[str, Any] = {"ok": True, "pid": os.getpid(), "version": __version__}
            nonce = parse_qs(parsed.query).get("nonce", [None])[0]
            token = self._daemon.token
            if nonce and token and len(nonce) <= 64:
                proof = hmac.new(
                    token.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256
                ).hexdigest()
                health["proof"] = proof
            self._send_json(200, health)
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self.path.startswith("/hooks/"):
            self._send_close(404, {"error": "not found"})
            return
        event = unquote(self.path[len("/hooks/") :])
        start = time.perf_counter()
        reason = self._reject_reason()
        if reason is not None:
            self._reject(event, reason, start, 0)
            return
        length = self._content_length()
        if length is None:
            self._send_close(400, {"error": "bad request"})
            return
        if length > MAX_BODY_BYTES:
            self._send_close(413, {"error": "too large"})
            return
        if not self._token_ok():
            self._reject(event, "token", start, 0, status=401, message="unauthorized")
            return
        body = self.rfile.read(length) if length else b""
        payload, tool_name, session_id = _parse_payload(body)
        if payload is None:
            self._finish(event, None, None, {}, "error", start, len(body))
            print(f"cfxd: malformed hook payload for {event}", file=sys.stderr)
            return
        try:
            future = _POOL.submit(dispatch, event, payload)
            result = future.result(timeout=HANDLER_TIMEOUT_MS / 1000.0)
        except FutureTimeout:
            self._finish(event, tool_name, session_id, {}, "error", start, len(body))
            print(
                f"cfxd: handler {event} exceeded {HANDLER_TIMEOUT_MS:.0f} ms; passed through",
                file=sys.stderr,
            )
            return
        except Exception as exc:
            self._finish(event, tool_name, session_id, {}, "error", start, len(body))
            print(f"cfxd: handler {event} failed: {type(exc).__name__}", file=sys.stderr)
            return
        self._finish(event, tool_name, session_id, result, "handled", start, len(body))

    def _finish(
        self,
        event: str,
        tool_name: str | None,
        session_id: str | None,
        result: dict[str, Any],
        outcome: str,
        start: float,
        bytes_in: int,
    ) -> None:
        data = json.dumps(result).encode("utf-8")
        self._daemon.log_event(
            {
                "ts": datetime.now(UTC).isoformat(),
                "event": event,
                "tool_name": tool_name,
                "session_id": session_id,
                "latency_ms": round((time.perf_counter() - start) * 1000.0, 3),
                "outcome": outcome,
                "bytes_in": bytes_in,
                "bytes_out": len(data),
            }
        )
        self._send_bytes(200, data)


def run_serve(state_dir: Path, port: int) -> int:
    os.umask(0o077)
    ensure_dir(state_dir)
    existing = read_state(state_dir / STATE_FILE)
    if existing is not None and daemon_alive(existing):
        print("already running")
        return 0
    if port != 0 and _health_ok(port):
        print("already running")
        return 0
    daemon = Daemon(state_dir, port)
    try:
        daemon.start()
    except OSError:
        print(f"cfxd: port {port} is already in use by another process", file=sys.stderr)
        return 2
    print(f"cfxd: listening on http://127.0.0.1:{daemon.port} (pid {os.getpid()})", flush=True)
    try:
        daemon.wait()
    except KeyboardInterrupt:
        daemon.stop()
    return 0
