"""Sidecar kv_hints wiring — default off is byte-identical, opt-in attaches the envelope."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

from kernel.router.kv_hints_envelope import ACTION_TYPE, HTTP_FIELD
from protocol.models import BlockKind, ContextBlock, Creator, CreatorType, Fidelity, MemoryTier
from sidecar.server import load_blocks, make_server

_SEEN_BODIES: list[dict[str, Any]] = []


class _RecordingUpstream(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        _SEEN_BODIES.append(json.loads(self.rfile.read(length)))
        body = json.dumps(
            {
                "id": "cmpl-test",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def _fabric_blocks(tmp_path: Path) -> tuple[ContextBlock, ...]:
    block = ContextBlock(
        block_id="d1",
        block_kind=BlockKind.decision,
        memory_tier=MemoryTier.ledger,
        content="Use Postgres for canonical storage",
        content_hash="sha256:d1",
        token_estimate=8,
        source_event_seq=1,
        creator=Creator(type=CreatorType.human, id="tester"),
        fidelity=Fidelity.extract,
        trust_domain="default",
    )
    path = tmp_path / "fabric.jsonl"
    path.write_text(json.dumps(block.model_dump(mode="json")) + "\n", encoding="utf-8")
    return load_blocks(path)


def _start(upstream: ThreadingHTTPServer) -> str:
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{upstream.server_address[1]}"


def _chat(base_url: str) -> httpx.Response:
    return httpx.post(
        f"{base_url}/v1/chat/completions",
        json={"model": "test", "messages": [{"role": "user", "content": "hello fabric"}]},
        timeout=10.0,
    )


def test_kv_hints_off_forwards_no_envelope(tmp_path: Path) -> None:
    _SEEN_BODIES.clear()
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingUpstream)
    sidecar = make_server(
        upstream=_start(upstream), mode="compile", blocks=_fabric_blocks(tmp_path), port=0
    )
    threading.Thread(target=sidecar.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{sidecar.server_address[1]}"
    try:
        assert _chat(url).status_code == 200
        (forwarded,) = _SEEN_BODIES
        assert HTTP_FIELD not in forwarded
        assert forwarded["messages"][0]["role"] == "system"  # compiled packet still injected
    finally:
        sidecar.shutdown()
        upstream.shutdown()


def test_kv_hints_on_attaches_envelope(tmp_path: Path) -> None:
    _SEEN_BODIES.clear()
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingUpstream)
    sidecar = make_server(
        upstream=_start(upstream),
        mode="compile",
        blocks=_fabric_blocks(tmp_path),
        port=0,
        kv_hints=True,
    )
    threading.Thread(target=sidecar.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{sidecar.server_address[1]}"
    try:
        assert _chat(url).status_code == 200
        (forwarded,) = _SEEN_BODIES
        envelope = forwarded[HTTP_FIELD]
        assert set(envelope) == {"protocol_version", "message_id", "actions"}
        (action,) = envelope["actions"]
        assert action["action_type"] == ACTION_TYPE
        assert set(action["payload"]) == {
            "session_id",
            "prefix_hash",
            "shared_prefix_hash",
            "ttl_seconds",
            "evict_priority",
        }
        assert action["payload"]["session_id"] == "chat:chat"
        assert action["payload"]["shared_prefix_hash"] == ""  # no cartridge in sidecar compile
    finally:
        sidecar.shutdown()
        upstream.shutdown()
