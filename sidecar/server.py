"""OpenAI-compatible sidecar proxy that injects compiled Context Fabric packets.

``compile`` mode compiles a packet from the last user message (objective) plus the
fabric JSONL blocks, prepends its rendered text as a system message, and forwards.
``passthrough`` forwards untouched, for honest A/B comparisons.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from kernel.router.compiler import compile_packet
from kernel.store import SqliteStore
from protocol.models import ContextBlock, Recipient, Scope
from sidecar.render import render_packet

_TIMEOUT = httpx.Timeout(600.0, connect=10.0)
_SKIP_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "accept-encoding",
        "transfer-encoding",
        # RFC 7230 §6.1 hop-by-hop headers.
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "upgrade",
    }
)
_SCOPE = Scope(tenant_id="default", project_id="default", workflow_id="chat", task_id="chat")


class Stats:
    """Thread-safe request counters exposed at /fabric/stats."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters = {
            "requests": 0,
            "compiled": 0,
            "errors": 0,
            "upstream_prompt_tokens": 0,
        }

    def add(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[key] += amount

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)


def load_blocks(path: Path) -> tuple[ContextBlock, ...]:
    """Load one ContextBlock JSON per line."""
    blocks: list[ContextBlock] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                blocks.append(ContextBlock.model_validate_json(line))
            except ValidationError as exc:
                raise ValueError(f"{path}:{lineno}: {exc}") from exc
    return tuple(blocks)


def load_fabric(path: Path) -> tuple[ContextBlock, ...]:
    """Load a fabric source: a SQLite store for ``.db``/``.sqlite`` paths, else JSONL."""
    if not path.is_file():
        raise FileNotFoundError(f"fabric file not found: {path}")
    if path.name.lower().endswith((".db", ".sqlite")):
        return tuple(SqliteStore(path).load_blocks())
    return load_blocks(path)


def _objective(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
            return ""
    return ""


def _prompt_tokens(body: bytes) -> int | None:
    try:
        usage = json.loads(body).get("usage") or {}
        tokens = usage.get("prompt_tokens")
        return int(tokens) if tokens is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _make_handler(
    upstream: str,
    mode: str,
    blocks: tuple[ContextBlock, ...],
    stats: Stats,
    client: httpx.Client,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            try:
                self._route_post()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self) -> None:
            try:
                self._route_get()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _route_post(self) -> None:
            if self.path.split("?", 1)[0] != "/v1/chat/completions":
                self._send_json(404, {"error": {"message": "not found"}})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0:
                    raise ValueError(length)
            except ValueError:
                self._send_json(400, {"error": {"message": "invalid Content-Length header"}})
                return
            body = self.rfile.read(length)
            stats.add("requests")
            extra: dict[str, str] = {}
            if mode == "compile":
                compiled = self._compile(body)
                if compiled is None:
                    return
                body, extra = compiled
            self._proxy("POST", f"{upstream}/v1/chat/completions", body, extra)

        def _route_get(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/fabric/health":
                self._send_json(200, {"status": "ok"})
            elif path == "/fabric/stats":
                self._send_json(200, stats.snapshot())
            elif path == "/v1/models":
                self._proxy("GET", f"{upstream}/v1/models", None, {})
            else:
                self._send_json(404, {"error": {"message": "not found"}})

        def _compile(self, body: bytes) -> tuple[bytes, dict[str, str]] | None:
            try:
                payload = json.loads(body)
            except ValueError:
                self._send_json(400, {"error": {"message": "request body must be JSON"}})
                return None
            if not isinstance(payload, dict):
                return body, {}
            objective = _objective(payload.get("messages"))
            if not objective:
                return body, {}
            result = compile_packet(
                scope=_SCOPE,
                recipient=Recipient(
                    agent_id=self.headers.get("X-Fabric-Agent", "default-agent"),
                    role=self.headers.get("X-Fabric-Role", "user"),
                    stage="chat",
                ),
                objective=objective,
                stage="chat",
                blocks=blocks,
                as_of=datetime.now(tz=UTC),
            )
            payload.setdefault("messages", []).insert(
                0, {"role": "system", "content": render_packet(result.packet)}
            )
            stats.add("compiled")
            return (
                json.dumps(payload).encode("utf-8"),
                {"X-Fabric-Compiled": "1", "X-Fabric-Packet-Id": result.packet.packet_id},
            )

        def _proxy(self, method: str, url: str, body: bytes | None, extra: dict[str, str]) -> None:
            skip = _SKIP_HEADERS | {
                token.strip().lower()
                for token in self.headers.get("Connection", "").split(",")
                if token.strip()
            }
            headers = {key: value for key, value in self.headers.items() if key.lower() not in skip}
            sent = False
            try:
                with client.stream(method, url, headers=headers, content=body) as resp:
                    sent = True
                    self._relay(resp, extra)
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                stats.add("errors")
                if not sent:
                    self._send_json(502, {"error": {"message": f"upstream unreachable: {exc}"}})

        def _relay(self, resp: httpx.Response, extra: dict[str, str]) -> None:
            content_type = resp.headers.get("content-type", "")
            self.send_response(resp.status_code)
            if content_type:
                self.send_header("Content-Type", content_type)
            for key, value in extra.items():
                self.send_header(key, value)
            if "text/event-stream" in content_type:
                self.end_headers()
                for chunk in resp.iter_bytes():
                    self.wfile.write(chunk)
                return
            payload = resp.read()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            tokens = _prompt_tokens(payload)
            if tokens is not None:
                stats.add("upstream_prompt_tokens", tokens)

        def _send_json(self, status: int, obj: Any) -> None:
            payload = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def make_server(
    *,
    upstream: str,
    mode: str,
    blocks: tuple[ContextBlock, ...] = (),
    port: int = 8080,
) -> ThreadingHTTPServer:
    client = httpx.Client(timeout=_TIMEOUT, trust_env=False)
    handler = _make_handler(upstream.rstrip("/"), mode, blocks, Stats(), client)
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def serve(*, upstream: str, port: int, fabric: Path | None, mode: str) -> None:
    """Load the fabric file (if any) and run the proxy until interrupted."""
    blocks = load_fabric(fabric) if fabric is not None else ()
    httpd = make_server(upstream=upstream, mode=mode, blocks=blocks, port=port)
    print(f"context-fabric sidecar: mode={mode} upstream={upstream} blocks={len(blocks)}")
    print(f"listening on http://127.0.0.1:{port}/v1")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
