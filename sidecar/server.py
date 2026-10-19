"""OpenAI-compatible sidecar proxy that injects compiled Context Fabric packets.

``compile`` mode compiles a packet from the last user message (objective) plus the
fabric blocks, prepends its rendered text as a system message, and forwards.
``passthrough`` forwards untouched, for honest A/B comparisons.

``POST /fabric/blocks`` appends a block to a SQLite fabric (writable memory). Written
blocks are quarantined as ``draft`` unless the request marks them trusted; draft blocks
are denied by the policy gate (reason ``lifecycle:draft``) until promoted at write time.
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from kernel.router.compiler import CompileResult, compile_packet
from kernel.router.hints import packet_cache_hints
from kernel.router.kv_hints_envelope import HTTP_FIELD, build_kv_hints_envelope
from kernel.store import CompileManifest, SqliteStore, StoreConflict
from protocol.models import ContextBlock, Recipient, Scope
from sidecar import b8, obsfold
from sidecar.blocks import build_block, stored_block
from sidecar.render import render_packet

_TIMEOUT = httpx.Timeout(600.0, connect=10.0)
_MAX_BODY_BYTES = 33_554_432
_STREAM_BUFFER_LIMIT = 262_144
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
_CORS_HEADERS = {
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Fabric-Agent,X-Fabric-Role",
    "Access-Control-Max-Age": "600",
}
_CORS_EXPOSED = "X-Fabric-Compiled,X-Fabric-Packet-Id"


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


def _max_body_bytes() -> int:
    try:
        value = int(os.environ.get("CF_MAX_BODY_BYTES", ""))
    except ValueError:
        return _MAX_BODY_BYTES
    return value if value > 0 else _MAX_BODY_BYTES


def _stats_payload(stats: Stats) -> dict[str, Any]:
    payload: dict[str, Any] = dict(stats.snapshot())
    price = os.environ.get("CF_PRICE_INPUT_PER_MTOK")
    if price:
        try:
            per_mtok = float(price)
        except ValueError:
            return payload
        if not math.isfinite(per_mtok):
            return payload
        payload["estimated_input_cost_usd"] = round(
            payload["upstream_prompt_tokens"] * per_mtok / 1_000_000, 6
        )
    return payload


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


def open_store(path: str | Path) -> SqliteStore:
    """Open a store whose single connection is shareable across server threads.

    Threading model: ``ThreadingHTTPServer`` runs one handler thread per connection over
    one shared ``sqlite3`` connection. Writes (including ``source_event_seq`` allocation
    and dedupe) are serialized by ``write_lock``; reads may interleave, which the
    serialized (``threadsafety == 3``) sqlite3 module supports.
    """
    return SqliteStore(path, check_same_thread=False)


def _open_fabric(path: Path) -> tuple[tuple[ContextBlock, ...], SqliteStore | None]:
    if not path.is_file():
        raise FileNotFoundError(f"fabric file not found: {path}")
    if path.name.lower().endswith((".db", ".sqlite")):
        store = open_store(path)
        return tuple(store.load_blocks()), store
    return load_blocks(path), None


def load_fabric(path: Path) -> tuple[ContextBlock, ...]:
    """Load a fabric source: a SQLite store for ``.db``/``.sqlite`` paths, else JSONL."""
    return _open_fabric(path)[0]


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


def _capture_stream_usage(line: bytes, stats: Stats) -> None:
    """Best-effort: count prompt tokens from a ``data:`` SSE line carrying a usage block."""
    if b'"usage"' not in line:
        return
    stripped = line.strip()
    if not stripped.startswith(b"data:"):
        return
    try:
        data = json.loads(stripped[5:].strip())
    except ValueError:
        return
    usage = data.get("usage") if isinstance(data, dict) else None
    tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    if isinstance(tokens, int):
        stats.add("upstream_prompt_tokens", tokens)


def _make_handler(
    upstream: str,
    mode: str,
    blocks: Sequence[ContextBlock],
    stats: Stats,
    client: httpx.Client,
    store: SqliteStore | None = None,
    *,
    obs_fold: bool = False,
    obs_store: SqliteStore | None = None,
    obs_n: int = obsfold.DEFAULT_N,
    obs_k: int = obsfold.DEFAULT_K,
    obs_max_tokens: int | None = None,
    window_cap: int | None = None,
    b8_ledger: b8.Ledger | None = None,
    b8_daily_cap: int = b8.DEFAULT_DAILY_CAP,
    kv_hints: bool = False,
) -> type[BaseHTTPRequestHandler]:
    fabric: list[ContextBlock] = list(blocks)
    # One shared sqlite3 connection: event_seq allocation plus append/dedupe must be
    # atomic across handler threads, so all writes take this lock.
    write_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def send_response(self, code: int, message: str | None = None) -> None:
            # Centralized so every path, including stdlib send_error(), carries CORS.
            super().send_response(code, message)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", _CORS_EXPOSED)

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

        def do_OPTIONS(self) -> None:
            try:
                self.send_response(204)
                for key, value in _CORS_HEADERS.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _read_body(self) -> bytes | None:
            if self.headers.get("Transfer-Encoding"):
                self._send_json(
                    411,
                    {
                        "error": {
                            "message": "Transfer-Encoding is not supported; send Content-Length"
                        }
                    },
                    close=True,
                )
                return None
            lengths = self.headers.get_all("Content-Length") or []
            if not lengths:
                self._send_json(
                    411, {"error": {"message": "Content-Length header is required"}}, close=True
                )
                return None
            if len(lengths) > 1:
                self._send_json(
                    400, {"error": {"message": "multiple Content-Length headers"}}, close=True
                )
                return None
            try:
                length = int(lengths[0])
                if length < 0:
                    raise ValueError(length)
            except ValueError:
                self._send_json(
                    400, {"error": {"message": "invalid Content-Length header"}}, close=True
                )
                return None
            cap = _max_body_bytes()
            if length > cap:
                self._drain_body(length)
                self._send_json(
                    413,
                    {"error": {"message": f"request body exceeds CF_MAX_BODY_BYTES ({cap} bytes)"}},
                    close=True,
                )
                return None
            return self.rfile.read(length)

        def _drain_body(self, length: int) -> None:
            """Discard an already-rejected body so the error close is a graceful FIN.

            On Windows, closing with unread inbound bytes is an abortive RST that can race
            the 413 off the wire (WinError 10053). Read in bounded chunks so a large
            Content-Length never allocates a matching buffer.
            shortcut: reads the full rejected body (up to Content-Length) before responding;
            a client that declares a huge length but never sends it stalls this handler —
            bound the drain if that ever becomes a real DoS.
            """
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 64 * 1024))
                if not chunk:
                    return
                remaining -= len(chunk)

        def _route_post(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/fabric/blocks":
                self._add_block()
                return
            if path != "/v1/chat/completions":
                self._send_json(404, {"error": {"message": "not found"}})
                return
            body = self._read_body()
            if body is None:
                return
            stats.add("requests")
            extra: dict[str, str] = {}
            if mode == "compile":
                compiled = self._compile(body)
                if compiled is None:
                    return
                body, extra = compiled
            if obs_fold:
                folded = self._obs_fold(body)
                if folded is None:
                    return
                body = folded
            if not self._b8_gate(body):
                return
            self._proxy("POST", f"{upstream}/v1/chat/completions", body, extra)

        def _route_get(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/fabric/health":
                self._send_json(200, {"status": "ok"})
            elif path == "/fabric/stats":
                self._send_json(200, _stats_payload(stats))
            elif path == "/v1/models":
                self._proxy("GET", f"{upstream}/v1/models", None, {})
            else:
                self._send_json(404, {"error": {"message": "not found"}})

        def _add_block(self) -> None:
            body = self._read_body()
            if body is None:
                return
            if store is None:
                self._send_json(
                    501,
                    {
                        "error": {
                            "message": "writable store not configured: start with --fabric "
                            "<path.db|path.sqlite>; JSONL fabrics are read-only"
                        }
                    },
                )
                return
            try:
                payload = json.loads(body)
            except ValueError:
                self._send_json(400, {"error": {"message": "request body must be JSON"}})
                return
            if not isinstance(payload, dict):
                self._send_json(400, {"error": {"message": "request body must be a JSON object"}})
                return
            with write_lock:
                try:
                    block = build_block(
                        payload,
                        trusted=payload.get("trusted") is True,
                        event_seq=store.max_source_event_seq() + 1,
                    )
                    inserted = store.append(block)
                except StoreConflict as exc:
                    self._send_json(409, {"error": {"message": str(exc)}})
                    return
                except ValueError as exc:
                    self._send_json(400, {"error": {"message": str(exc)}})
                    return
                if inserted:
                    fabric.append(block)
                else:
                    block = stored_block(store, block.block_id) or block
            result: dict[str, Any] = {
                "block_id": block.block_id,
                "content_hash": block.content_hash,
                "lifecycle_status": block.lifecycle_status.value,
                "source_event_seq": block.source_event_seq,
            }
            if inserted:
                self._send_json(201, result)
            else:
                self._send_json(200, {**result, "deduplicated": True})

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
            as_of = datetime.now(tz=UTC)
            recipient = Recipient(
                agent_id=self.headers.get("X-Fabric-Agent", "default-agent"),
                role=self.headers.get("X-Fabric-Role", "user"),
                stage="chat",
            )
            with write_lock:
                snapshot = tuple(fabric)
            purpose = self.headers.get("X-Fabric-Purpose")
            result = compile_packet(
                scope=_SCOPE,
                recipient=recipient,
                objective=objective,
                stage="chat",
                blocks=snapshot,
                watermark_event_seq=0,
                budget_base=4000,
                as_of=as_of,
                purpose=purpose,
            )
            self._log_compile(result, recipient, objective, as_of, snapshot, purpose=purpose)
            payload.setdefault("messages", []).insert(
                0, {"role": "system", "content": render_packet(result.packet)}
            )
            if kv_hints:
                payload[HTTP_FIELD] = build_kv_hints_envelope(packet_cache_hints(result.packet))
            stats.add("compiled")
            return (
                json.dumps(payload).encode("utf-8"),
                {"X-Fabric-Compiled": "1", "X-Fabric-Packet-Id": result.packet.packet_id},
            )

        def _obs_fold(self, body: bytes) -> bytes | None:
            """Fold older tool observations into reversible stubs before proxying.

            Runs only when obs-fold is enabled. Folding never breaks proxying: a malformed
            payload is forwarded untouched (it is already validated by ``_read_body`` only
            for framing, not JSON shape). The obs store writes are serialized with the
            fabric writes so the shared connection is not interleaved.
            """
            try:
                payload = json.loads(body)
            except ValueError:
                return body
            if not isinstance(payload, dict):
                return body
            messages = payload.get("messages")
            if not isinstance(messages, list):
                return body
            session_id = self.headers.get("X-Fabric-Agent", "default-agent")
            created_at = datetime.now(tz=UTC)
            if obs_store is None:
                result = obsfold.fold_messages(
                    messages,
                    n=obs_n,
                    k=obs_k,
                    max_context_tokens=obs_max_tokens,
                    session_id=session_id,
                    created_at=created_at,
                )
            else:
                with write_lock:
                    result = obsfold.fold_messages(
                        messages,
                        n=obs_n,
                        k=obs_k,
                        max_context_tokens=obs_max_tokens,
                        store=obs_store,
                        session_id=session_id,
                        created_at=created_at,
                    )
            if not result.folded:
                return body
            payload["messages"] = result.messages
            return json.dumps(payload).encode("utf-8")

        def _b8_gate(self, body: bytes) -> bool:
            """Model allow-list, window cap, and daily cap — all before any upstream spend.

            Returns ``False`` after sending a rejection response, ``True`` to forward.
            Off (no window cap, no ledger) it is a no-op, so passthrough stays
            byte-identical. A non-JSON or non-object body is forwarded untouched (it is
            already framed, not validated, by ``_read_body``).
            """
            if window_cap is None and b8_ledger is None:
                return True
            try:
                payload = json.loads(body)
            except ValueError:
                return True
            if not isinstance(payload, dict):
                return True
            messages = payload.get("messages")
            if not isinstance(messages, list):
                return True
            model = payload.get("model")
            if b8_ledger is not None:
                if not isinstance(model, str) or not b8.is_allowed_model(model):
                    self._send_json(
                        400,
                        {
                            "error": {
                                "message": (
                                    f"model {model!r} is not in the B8 allow-list; use one "
                                    f"of {b8.ALLOWED_MODELS}"
                                ),
                                "type": "invalid_request_error",
                                "param": "model",
                                "code": "model_not_allowed",
                            }
                        },
                    )
                    return False
            input_tokens = b8.estimate_messages_tokens(messages)
            max_tokens = b8.max_completion_tokens(payload)
            if window_cap is not None and input_tokens + max_tokens > window_cap:
                self._send_json(400, b8.context_window_error(input_tokens, max_tokens, window_cap))
                return False
            if b8_ledger is not None:
                day = b8.utc_day()
                used = b8_ledger.day_total(day)
                max_possible = input_tokens + max_tokens
                if used + max_possible > b8_daily_cap:
                    self._send_json(400, b8.daily_cap_error(used, max_possible, b8_daily_cap, day))
                    return False
            return True

        def _record_usage(self, payload: bytes) -> None:
            """Append an upstream response's usage to the daily ledger (money safety)."""
            if b8_ledger is None:
                return
            try:
                data = json.loads(payload)
            except ValueError:
                return
            if not isinstance(data, dict):
                return
            usage = b8.parse_usage(data)
            if "prompt_tokens" not in usage:
                return
            model = data.get("model")
            b8_ledger.append(
                b8.utc_day(),
                model=model if isinstance(model, str) else "",
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                reasoning_tokens=usage.get("reasoning_tokens", 0),
                cached_tokens=usage.get("cached_tokens", 0),
            )

        def _log_compile(
            self,
            result: CompileResult,
            recipient: Recipient,
            objective: str,
            as_of: datetime,
            blocks: Sequence[ContextBlock],
            purpose: str | None = None,
        ) -> None:
            """Record the compile manifest for ``cfx replay``; failure never breaks proxying.

            ``blocks`` is the exact snapshot passed to ``compile_packet`` (never the live
            ``fabric`` list), so a concurrent ``POST /fabric/blocks`` cannot make the
            manifest disagree with what was compiled. Only a writable store has somewhere
            to log: JSONL fabrics and no-fabric runs skip silently. ``model``/``engine`` are
            operator metadata (``CF_MODEL``/``CF_ENGINE``, "" when unset), not probed from
            the upstream.

            ``packet_id`` excludes ``as_of``, so a repeat compile of the same
            objective/agent/watermark reuses the id. Only the first manifest per id is
            kept (CD-06); a repeat is proxied normally but reported on stderr instead of
            being silently un-receipted.
            """
            if store is None:
                return
            pairs = sorted({block.block_id: block.content_hash for block in blocks}.items())
            manifest = CompileManifest(
                packet_id=result.packet.packet_id,
                content_hash=result.packet.hashes.content_hash,
                scope=_SCOPE,
                recipient=recipient,
                objective=objective,
                stage="chat",
                as_of=as_of,
                watermark_event_seq=0,
                budget_base=4000,
                block_ids=tuple(block_id for block_id, _ in pairs),
                block_hashes=tuple(content_hash for _, content_hash in pairs),
                model=os.environ.get("CF_MODEL", ""),
                engine=os.environ.get("CF_ENGINE", ""),
                purpose=purpose,
                created_at=datetime.now(tz=UTC),
            )
            try:
                with write_lock:
                    existing = store.get_compile(manifest.packet_id)
                    if existing is not None:
                        print(
                            f"sidecar: packet {manifest.packet_id} already receipted"
                            f" ({existing.content_hash}); this compile"
                            f" ({manifest.content_hash}) is proxied but not re-logged"
                            " (first receipt wins)",
                            file=sys.stderr,
                        )
                        return
                    store.log_compile(manifest)
            except Exception as exc:  # a log failure must never break proxying
                print(
                    f"sidecar: compile log write failed for {manifest.packet_id}: {exc}",
                    file=sys.stderr,
                )

        def _proxy(self, method: str, url: str, body: bytes | None, extra: dict[str, str]) -> None:
            skip = _SKIP_HEADERS | {
                token.strip().lower()
                for token in self.headers.get("Connection", "").split(",")
                if token.strip()
            }
            headers = {key: value for key, value in self.headers.items() if key.lower() not in skip}
            self._response_started = False
            try:
                with client.stream(method, url, headers=headers, content=body) as resp:
                    self._relay(resp, extra)
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                stats.add("errors")
                if not self._response_started:
                    self._send_json(502, {"error": {"message": f"upstream unreachable: {exc}"}})
                else:
                    # Response already started mid-relay; close instead of desyncing keep-alive.
                    self.close_connection = True

        def _relay(self, resp: httpx.Response, extra: dict[str, str]) -> None:
            content_type = resp.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                self._start_relay(resp.status_code, content_type, extra)
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                self._relay_stream(resp)
                return
            # Read before starting the response so an upstream death maps to a clean 502.
            payload = resp.read()
            self._start_relay(resp.status_code, content_type, extra)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            tokens = _prompt_tokens(payload)
            if tokens is not None:
                stats.add("upstream_prompt_tokens", tokens)
            self._record_usage(payload)

        def _start_relay(self, status: int, content_type: str, extra: dict[str, str]) -> None:
            self._response_started = True
            self.send_response(status)
            if content_type:
                self.send_header("Content-Type", content_type)
            for key, value in extra.items():
                self.send_header(key, value)

        def _relay_stream(self, resp: httpx.Response) -> None:
            """Pipe SSE bytes through unchanged; count usage and report mid-stream death."""
            pending = b""
            try:
                for chunk in resp.iter_bytes():
                    self.wfile.write(chunk)
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        _capture_stream_usage(line, stats)
                    if len(pending) > _STREAM_BUFFER_LIMIT:
                        pending = b""
            except httpx.HTTPError:
                stats.add("errors")
                marker = (b"\n" if pending else b"") + b": fabric: upstream stream aborted\n\n"
                try:
                    self.wfile.write(marker)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            _capture_stream_usage(pending, stats)

        def _send_json(self, status: int, obj: Any, *, close: bool = False) -> None:
            payload = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(payload)

    return Handler


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer sized for bursty agent fleets.

    The socketserver default listen backlog is 5; a simultaneous burst of >5 client
    connections (measured with ``evals/bench_sidecar.py`` at N=64: refused connections and
    ~1 s SYN-retransmit stalls on Windows) overflows it. 256 costs nothing and absorbs the
    burst; ``daemon_threads``/``allow_reuse_address`` are already True/1 in the stdlib base.
    """

    request_queue_size = 256


def make_server(
    *,
    upstream: str,
    mode: str,
    blocks: tuple[ContextBlock, ...] = (),
    port: int = 8080,
    store: SqliteStore | None = None,
    obs_fold: bool = False,
    obs_store: SqliteStore | None = None,
    obs_n: int = obsfold.DEFAULT_N,
    obs_k: int = obsfold.DEFAULT_K,
    obs_max_tokens: int | None = None,
    window_cap: int | None = None,
    b8_ledger: b8.Ledger | None = None,
    b8_daily_cap: int = b8.DEFAULT_DAILY_CAP,
    kv_hints: bool = False,
) -> ThreadingHTTPServer:
    client = httpx.Client(timeout=_TIMEOUT, trust_env=False)
    handler = _make_handler(
        upstream.rstrip("/"),
        mode,
        blocks,
        Stats(),
        client,
        store,
        obs_fold=obs_fold,
        obs_store=obs_store,
        obs_n=obs_n,
        obs_k=obs_k,
        obs_max_tokens=obs_max_tokens,
        window_cap=window_cap,
        b8_ledger=b8_ledger,
        b8_daily_cap=b8_daily_cap,
        kv_hints=kv_hints,
    )
    return _Server(("127.0.0.1", port), handler)


def serve(
    *,
    upstream: str,
    port: int,
    fabric: Path | None,
    mode: str,
    obs_fold: bool = False,
    obs_store: SqliteStore | None = None,
    obs_n: int = obsfold.DEFAULT_N,
    obs_k: int = obsfold.DEFAULT_K,
    obs_max_tokens: int | None = None,
    window_cap: int | None = None,
    b8_ledger: b8.Ledger | None = None,
    b8_daily_cap: int = b8.DEFAULT_DAILY_CAP,
    kv_hints: bool = False,
) -> None:
    """Load the fabric file (if any) and run the proxy until interrupted."""
    blocks: tuple[ContextBlock, ...] = ()
    store: SqliteStore | None = None
    if fabric is not None:
        blocks, store = _open_fabric(fabric)
    httpd = make_server(
        upstream=upstream,
        mode=mode,
        blocks=blocks,
        port=port,
        store=store,
        obs_fold=obs_fold,
        obs_store=obs_store,
        obs_n=obs_n,
        obs_k=obs_k,
        obs_max_tokens=obs_max_tokens,
        window_cap=window_cap,
        b8_ledger=b8_ledger,
        b8_daily_cap=b8_daily_cap,
        kv_hints=kv_hints,
    )
    print(f"context-fabric sidecar: mode={mode} upstream={upstream} blocks={len(blocks)}")
    print(f"listening on http://127.0.0.1:{port}/v1")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
