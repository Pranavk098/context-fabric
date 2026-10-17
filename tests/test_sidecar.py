"""Sidecar tests — fake upstream on an ephemeral port, no external network, no GPU."""

from __future__ import annotations

import gzip
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from kernel.store import SqliteStore
from protocol.models import BlockKind, ContextBlock, Creator, CreatorType, Fidelity, MemoryTier
from sidecar.__main__ import main
from sidecar.server import load_blocks, load_fabric, make_server

_SEEN_HEADERS: list[dict[str, str]] = []


class _FakeUpstream(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        _SEEN_HEADERS.append(dict(self.headers.items()))
        if request.get("model") == "gzip":
            body = gzip.compress(
                b'data: {"choices":[{"delta":{"content":"zipped"}}]}\n\ndata: [DONE]\n\n'
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for word in ("hello", " fabric"):
                delta = {"choices": [{"index": 0, "delta": {"content": word}}]}
                self.wfile.write(f"data: {json.dumps(delta)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        payload = {
            "id": "cmpl-test",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": request["messages"][0]["content"]},
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8},
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        body = json.dumps({"object": "list", "data": [{"id": "test-model", "object": "model"}]})
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def _serve(server: ThreadingHTTPServer) -> str:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


def _start_upstream() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
    return server, _serve(server)


def _start_sidecar(
    upstream_url: str, mode: str, blocks: tuple[ContextBlock, ...] = ()
) -> tuple[ThreadingHTTPServer, str]:
    server = make_server(upstream=upstream_url, mode=mode, blocks=blocks, port=0)
    return server, _serve(server)


def _stop(*servers: ThreadingHTTPServer) -> None:
    for server in servers:
        server.shutdown()
        server.server_close()


def _chat(base_url: str, **kwargs: Any) -> httpx.Response:
    return httpx.post(
        f"{base_url}/v1/chat/completions",
        json={"model": "test", "messages": [{"role": "user", "content": "hello fabric"}]},
        timeout=10.0,
        **kwargs,
    )


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


def test_passthrough_forwards_body_and_status_unchanged() -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url = _start_sidecar(upstream_url, "passthrough")
    try:
        response = _chat(sidecar_url)
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "hello fabric"
        assert "x-fabric-compiled" not in response.headers

        models = httpx.get(f"{sidecar_url}/v1/models", timeout=10.0)
        assert models.status_code == 200
        assert models.json()["data"][0]["id"] == "test-model"
    finally:
        _stop(sidecar, upstream)


def test_compile_injects_packet_and_headers(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url = _start_sidecar(upstream_url, "compile", _fabric_blocks(tmp_path))
    try:
        response = _chat(sidecar_url, headers={"X-Fabric-Agent": "a1", "X-Fabric-Role": "builder"})
        assert response.status_code == 200
        assert response.headers["x-fabric-compiled"] == "1"
        assert response.headers["x-fabric-packet-id"].startswith("cpk_")
        content = response.json()["choices"][0]["message"]["content"]
        assert "OBJECTIVE: hello fabric" in content
        assert "Use Postgres for canonical storage" in content
    finally:
        _stop(sidecar, upstream)


def test_compile_mode_loads_sqlite_fabric(tmp_path: Path) -> None:
    (block,) = _fabric_blocks(tmp_path)
    db = tmp_path / "fabric.db"
    assert SqliteStore(db).append(block)
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url = _start_sidecar(upstream_url, "compile", load_fabric(db))
    try:
        response = _chat(sidecar_url)
        assert response.status_code == 200
        assert response.headers["x-fabric-compiled"] == "1"
        content = response.json()["choices"][0]["message"]["content"]
        assert "Use Postgres for canonical storage" in content
    finally:
        _stop(sidecar, upstream)


def test_load_fabric_missing_file_raises_and_does_not_create(tmp_path: Path) -> None:
    missing_db = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        load_fabric(missing_db)
    assert not missing_db.exists()
    with pytest.raises(FileNotFoundError):
        load_fabric(tmp_path / "missing.jsonl")


def test_compile_mode_streams_sse_passthrough(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url = _start_sidecar(upstream_url, "compile", _fabric_blocks(tmp_path))
    try:
        response = httpx.post(
            f"{sidecar_url}/v1/chat/completions",
            json={
                "model": "test",
                "stream": True,
                "messages": [{"role": "user", "content": "hello fabric"}],
            },
            timeout=10.0,
        )
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        assert response.headers["x-fabric-compiled"] == "1"
        assert '"hello"' in response.text
        assert "data: [DONE]" in response.text
    finally:
        _stop(sidecar, upstream)


def test_stats_counters_increment(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url = _start_sidecar(upstream_url, "compile", _fabric_blocks(tmp_path))
    try:
        assert httpx.get(f"{sidecar_url}/fabric/health", timeout=10.0).json() == {"status": "ok"}
        _chat(sidecar_url)
        stats = httpx.get(f"{sidecar_url}/fabric/stats", timeout=10.0).json()
        assert stats == {
            "requests": 1,
            "compiled": 1,
            "errors": 0,
            "upstream_prompt_tokens": 7,
        }
    finally:
        _stop(sidecar, upstream)


def test_invalid_content_length_returns_400() -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, _ = _start_sidecar(upstream_url, "passthrough")
    try:
        with socket.create_connection(("127.0.0.1", sidecar.server_address[1]), timeout=10.0) as s:
            s.sendall(
                b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                b"Content-Length: notanumber\r\n\r\n{}"
            )
            data = s.recv(4096)
        assert b" 400 " in data.split(b"\r\n", 1)[0]
    finally:
        _stop(sidecar, upstream)


def test_gzip_sse_upstream_is_decoded() -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url = _start_sidecar(upstream_url, "passthrough")
    try:
        response = httpx.post(
            f"{sidecar_url}/v1/chat/completions",
            json={"model": "gzip", "stream": True, "messages": []},
            timeout=10.0,
        )
        assert response.status_code == 200
        assert '"zipped"' in response.text
        assert "data: [DONE]" in response.text
    finally:
        _stop(sidecar, upstream)


def test_connection_listed_hop_headers_are_stripped() -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, _ = _start_sidecar(upstream_url, "passthrough")
    try:
        with socket.create_connection(("127.0.0.1", sidecar.server_address[1]), timeout=10.0) as s:
            s.sendall(
                b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                b"Connection: close, X-Custom-Hop\r\nX-Custom-Hop: smuggled\r\n"
                b"Content-Length: 2\r\n\r\n{}"
            )
            s.recv(4096)
        assert "X-Custom-Hop" not in _SEEN_HEADERS[-1]
    finally:
        _stop(sidecar, upstream)


def test_bad_upstream_url_returns_502() -> None:
    sidecar = make_server(upstream="http://bad\x01host", mode="passthrough", port=0)
    sidecar_url = _serve(sidecar)
    try:
        response = httpx.post(
            f"{sidecar_url}/v1/chat/completions", json={"model": "m", "messages": []}, timeout=10.0
        )
        assert response.status_code == 502
    finally:
        _stop(sidecar)


def test_invalid_cf_mode_env_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["python -m sidecar"])
    monkeypatch.setenv("CF_MODE", "bogus")
    with pytest.raises(SystemExit):
        main()
