"""Resident Context Fabric daemon: a 127.0.0.1 hook endpoint that fails open.

Any handler error or slow handler returns ``{}`` with HTTP 200, so a hook can
never block the agent. Requests are logged to ``events.jsonl`` (metadata only —
never payload content, which may contain user code).
"""

from __future__ import annotations

import json
import os
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
from urllib.error import HTTPError
from urllib.parse import unquote
from urllib.request import Request, urlopen

from cfxd import __version__, session_state
from cfxd.brief import install as brief_install
from cfxd.carry import install as carry_install
from cfxd.fold import install
from cfxd.handlers import add_observer, dispatch

STATE_FILE = "state.json"
EVENTS_FILE = "events.jsonl"
HANDLER_TIMEOUT_MS = 150.0
HEALTH_TIMEOUT_S = 0.5
DEFAULT_PORT = 47831
DEFAULT_STATE_DIR = Path.home() / ".cfx" / "daemon"

# Handlers run here so a slow one is abandoned at the deadline instead of holding the hook.
_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="cfxd-handler")


def resolve_state_dir(explicit: str | None = None) -> Path:
    """``--state-dir`` > ``CFX_HOME`` > ``%USERPROFILE%/.cfx/daemon``."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("CFX_HOME")
    if env:
        return Path(env)
    return DEFAULT_STATE_DIR


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
    state_dir.mkdir(parents=True, exist_ok=True)
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


def get_health(port: int, timeout: float = HEALTH_TIMEOUT_S) -> tuple[int, bytes]:
    try:
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as resp:
            return resp.status, resp.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def post_hook(port: int, event: str, payload: bytes, timeout: float = 1.0) -> tuple[int, bytes]:
    req = Request(
        f"http://127.0.0.1:{port}/hooks/{event}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def _health_ok(port: int) -> bool:
    try:
        status, body = get_health(port)
    except OSError:
        return False
    if status != 200:
        return False
    try:
        health = json.loads(body)
    except ValueError:
        return False
    return isinstance(health, dict) and health.get("ok") is True


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
        self._httpd: _CfxdServer | None = None
        self._thread: threading.Thread | None = None
        self._log_lock = threading.Lock()

    def start(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        install(self.state_dir)
        brief_install(self.state_dir)
        carry_install(self.state_dir)
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
            with self.events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")


class _CfxdServer(ThreadingHTTPServer):
    daemon: Daemon

    if os.name == "nt":
        # On Windows SO_REUSEADDR lets a second server bind the same port, so two
        # sessions starting together could run two daemons. Bind exclusively instead.
        allow_reuse_address = False

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

    def _reject(self, event: str, reason: str, start: float, bytes_in: int) -> None:
        self._daemon.log_event(
            {
                "ts": datetime.now(UTC).isoformat(),
                "event": event,
                "tool_name": None,
                "session_id": None,
                "latency_ms": round((time.perf_counter() - start) * 1000.0, 3),
                "outcome": "rejected",
                "reason": reason,
                "bytes_in": bytes_in,
                "bytes_out": 0,
            }
        )
        self._send_json(403, {"error": "forbidden"})

    def _read_body(self) -> bytes:
        raw = self.headers.get("Content-Length")
        if raw is None:
            return b""
        try:
            length = int(raw)
        except ValueError:
            return b""
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def do_GET(self) -> None:
        if not self._host_ok():
            self._send_json(403, {"error": "forbidden"})
            return
        if self.path == "/health":
            self._send_json(200, {"ok": True, "pid": os.getpid(), "version": __version__})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        body = self._read_body()
        if not self.path.startswith("/hooks/"):
            self._send_json(404, {"error": "not found"})
            return
        event = unquote(self.path[len("/hooks/") :])
        start = time.perf_counter()
        reason = self._reject_reason()
        if reason is not None:
            self._reject(event, reason, start, len(body))
            return
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
            print(f"cfxd: handler {event} failed: {exc!r}", file=sys.stderr)
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
    state_dir.mkdir(parents=True, exist_ok=True)
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
