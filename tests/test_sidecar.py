"""Sidecar tests — fake upstream on an ephemeral port, no external network, no GPU."""

from __future__ import annotations

import concurrent.futures
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
from sidecar.server import _objective, load_blocks, load_fabric, make_server, open_store

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
        if request.get("model") == "usage-stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            delta = {"choices": [{"index": 0, "delta": {"content": "counted"}}]}
            usage = {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 1}}
            self.wfile.write(f"data: {json.dumps(delta)}\n\n".encode())
            self.wfile.write(f"data: {json.dumps(usage)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        if request.get("model") == "dying-json":
            self.protocol_version = "HTTP/1.1"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b'{"partial":')
            self.wfile.flush()
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return
        if request.get("model") == "dying":
            self.protocol_version = "HTTP/1.1"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            chunk = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
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
    upstream_url: str,
    mode: str,
    blocks: tuple[ContextBlock, ...] = (),
    store: SqliteStore | None = None,
) -> tuple[ThreadingHTTPServer, str]:
    server = make_server(upstream=upstream_url, mode=mode, blocks=blocks, port=0, store=store)
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


def test_bare_models_path_is_proxied() -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url = _start_sidecar(upstream_url, "passthrough")
    try:
        response = httpx.get(f"{sidecar_url}/models", timeout=10.0)
        assert response.status_code == 200
        assert response.json()["data"][0]["id"] == "test-model"
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


def test_objective_skips_agent_tool_reminders() -> None:
    task = "Fix the failing test in tests/test_foo.py"
    format_reminder = (
        "Tool call error:\n\n<error>\nNo tool calls found in the response. Every "
        "response MUST include at least one tool call.\n</error>\n\nHere is general guidance..."
    )
    truncation_reminder = (
        "Your previous response reached the output token limit (finish_reason=length) "
        "before you produced a tool call, so it was cut off."
    )
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": task},
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "tool", "tool_call_id": "call_1", "content": "<returncode>1</returncode>"},
        {"role": "user", "content": format_reminder},
    ]
    assert _objective(messages) == task
    assert (
        _objective(
            [{"role": "user", "content": task}, {"role": "user", "content": truncation_reminder}]
        )
        == task
    )
    # A reminder-only list has no substantive objective.
    assert _objective([{"role": "user", "content": format_reminder}]) == ""


def test_objective_unchanged_without_reminders() -> None:
    # Regression: for reminder-free lists the heuristic is byte-identical to the
    # pre-change rule — the exact content of the last user message, verbatim.
    assert _objective([{"role": "user", "content": "hello fabric"}]) == "hello fabric"
    assert (
        _objective([{"role": "user", "content": "first"}, {"role": "user", "content": "last"}])
        == "last"
    )
    assert _objective([{"role": "user", "content": [{"text": "a"}, {"text": "b"}]}]) == "a b"
    assert _objective([{"role": "user", "content": ""}]) == ""
    assert _objective([{"role": "user", "content": None}]) == ""


def test_compile_reminder_and_clean_are_byte_identical(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url = _start_sidecar(upstream_url, "compile", _fabric_blocks(tmp_path))
    try:
        task = "Fix the failing test in tests/test_foo.py"
        reminder = (
            "Tool call error:\n\n<error>\nNo tool calls found in the response.\n</error>\n\n"
            "Here is general guidance..."
        )

        def compiled(messages: list[dict[str, Any]]) -> tuple[str, str]:
            response = httpx.post(
                f"{sidecar_url}/v1/chat/completions",
                json={"model": "test", "messages": messages},
                timeout=10.0,
            )
            return response.headers["x-fabric-packet-id"], response.json()["choices"][0]["message"][
                "content"
            ]

        pid_clean, content_clean = compiled([{"role": "user", "content": task}])
        pid_reminder, content_reminder = compiled(
            [{"role": "user", "content": task}, {"role": "user", "content": reminder}]
        )
        assert pid_clean == pid_reminder
        assert content_clean == content_reminder
        assert f"OBJECTIVE: {task}" in content_clean
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


def _post_block(base_url: str, payload: dict[str, Any]) -> httpx.Response:
    return httpx.post(f"{base_url}/fabric/blocks", json=payload, timeout=10.0)


def _writable_sidecar(
    tmp_path: Path, upstream_url: str, mode: str = "compile"
) -> tuple[ThreadingHTTPServer, str, Path]:
    db = tmp_path / "fabric.db"
    server, url = _start_sidecar(upstream_url, mode, store=open_store(db))
    return server, url, db


def test_post_block_creates_draft_and_persists(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url, db = _writable_sidecar(tmp_path, upstream_url)
    try:
        response = _post_block(sidecar_url, {"block_kind": "decision", "content": "Use Postgres"})
        assert response.status_code == 201
        data = response.json()
        assert data["block_id"].startswith("blk_")
        assert data["content_hash"].startswith("sha256:")
        assert data["lifecycle_status"] == "draft"
        assert data["source_event_seq"] == 1
        (stored,) = SqliteStore(db).load_blocks()
        assert stored.content == "Use Postgres"
        assert stored.lifecycle_status.value == "draft"
    finally:
        _stop(sidecar, upstream)


def test_draft_block_is_quarantined_from_compile(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url, _ = _writable_sidecar(tmp_path, upstream_url)
    try:
        assert (
            _post_block(
                sidecar_url, {"block_kind": "fact", "content": "QUARANTINED-SECRET"}
            ).status_code
            == 201
        )
        response = _chat(sidecar_url)
        content = response.json()["choices"][0]["message"]["content"]
        assert "OBJECTIVE: hello fabric" in content
        assert "QUARANTINED-SECRET" not in content
    finally:
        _stop(sidecar, upstream)


def test_trusted_block_is_included_in_compile(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url, _ = _writable_sidecar(tmp_path, upstream_url)
    try:
        response = _post_block(
            sidecar_url,
            {"block_kind": "decision", "content": "Use Postgres", "trusted": True},
        )
        assert response.status_code == 201
        assert response.json()["lifecycle_status"] == "routable"
        content = _chat(sidecar_url).json()["choices"][0]["message"]["content"]
        assert "Use Postgres" in content
    finally:
        _stop(sidecar, upstream)


def test_post_block_deduplicates_and_conflicts(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url, _ = _writable_sidecar(tmp_path, upstream_url, mode="passthrough")
    try:
        first = _post_block(sidecar_url, {"block_kind": "fact", "content": "one"})
        assert first.status_code == 201
        again = _post_block(sidecar_url, {"block_kind": "fact", "content": "one"})
        assert again.status_code == 200
        assert again.json()["deduplicated"] is True
        assert again.json()["block_id"] == first.json()["block_id"]
        conflict = _post_block(
            sidecar_url,
            {"block_kind": "fact", "content": "two", "block_id": first.json()["block_id"]},
        )
        assert conflict.status_code == 409
        assert first.json()["block_id"] in conflict.json()["error"]["message"]
    finally:
        _stop(sidecar, upstream)


def test_post_block_without_store_returns_501(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url = _start_sidecar(upstream_url, "compile", _fabric_blocks(tmp_path))
    try:
        response = _post_block(sidecar_url, {"block_kind": "fact", "content": "x"})
        assert response.status_code == 501
        assert "--fabric" in response.json()["error"]["message"]
    finally:
        _stop(sidecar, upstream)


def test_post_block_bad_payload_returns_400(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url, _ = _writable_sidecar(tmp_path, upstream_url, mode="passthrough")
    try:
        assert _post_block(sidecar_url, {"block_kind": "fact"}).status_code == 400
        assert _post_block(sidecar_url, {"content": "no kind"}).status_code == 400
        assert (
            _post_block(sidecar_url, {"block_kind": "not-a-kind", "content": "x"}).status_code
            == 400
        )
        assert (
            _post_block(
                sidecar_url, {"block_kind": "fact", "content": "x", "ref_uri": "both"}
            ).status_code
            == 400
        )
        bad_json = httpx.post(f"{sidecar_url}/fabric/blocks", content=b"{not json", timeout=10.0)
        assert bad_json.status_code == 400
    finally:
        _stop(sidecar, upstream)


def test_request_body_over_cap_returns_413(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CF_MAX_BODY_BYTES", "16")
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url, _ = _writable_sidecar(tmp_path, upstream_url, mode="passthrough")
    try:
        response = _chat(sidecar_url)
        assert response.status_code == 413
        assert "CF_MAX_BODY_BYTES" in response.json()["error"]["message"]
    finally:
        _stop(sidecar, upstream)


def test_options_preflight_returns_204_with_cors(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url = _start_sidecar(upstream_url, "passthrough")
    try:
        response = httpx.request(
            "OPTIONS",
            f"{sidecar_url}/v1/chat/completions",
            headers={"Origin": "http://localhost", "Access-Control-Request-Method": "POST"},
            timeout=10.0,
        )
        assert response.status_code == 204
        assert response.headers["access-control-allow-origin"] == "*"
        assert "POST" in response.headers["access-control-allow-methods"]
        assert "X-Fabric-Agent" in response.headers["access-control-allow-headers"]
        assert "X-Fabric-Role" in response.headers["access-control-allow-headers"]
        assert response.headers["access-control-max-age"] == "600"
    finally:
        _stop(sidecar, upstream)


def test_cors_header_on_local_and_relayed_responses(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url = _start_sidecar(upstream_url, "passthrough")
    try:
        health = httpx.get(f"{sidecar_url}/fabric/health", timeout=10.0)
        assert health.headers["access-control-allow-origin"] == "*"
        assert _chat(sidecar_url).headers["access-control-allow-origin"] == "*"
    finally:
        _stop(sidecar, upstream)


def _read_http_response(sock: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        data += sock.recv(4096)
    head, _, body = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    while len(body) < length:
        body += sock.recv(4096)
    return head + b"\r\n\r\n" + body


def test_http11_keepalive_serves_two_requests_on_one_socket(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, _ = _start_sidecar(upstream_url, "passthrough")
    try:
        with socket.create_connection(("127.0.0.1", sidecar.server_address[1]), timeout=10.0) as s:
            s.sendall(b"GET /fabric/health HTTP/1.1\r\nHost: x\r\n\r\n")
            first = _read_http_response(s)
            s.sendall(b"GET /fabric/health HTTP/1.1\r\nHost: x\r\n\r\n")
            second = _read_http_response(s)
        assert b" 200 " in first.split(b"\r\n", 1)[0]
        assert b" 200 " in second.split(b"\r\n", 1)[0]
    finally:
        _stop(sidecar, upstream)


def test_stats_includes_estimated_cost_when_priced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CF_PRICE_INPUT_PER_MTOK", "2.0")
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url = _start_sidecar(upstream_url, "passthrough")
    try:
        _chat(sidecar_url)
        stats = httpx.get(f"{sidecar_url}/fabric/stats", timeout=10.0).json()
        assert stats["upstream_prompt_tokens"] == 7
        assert stats["estimated_input_cost_usd"] == 0.000014
    finally:
        _stop(sidecar, upstream)


def test_streaming_usage_is_captured() -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url = _start_sidecar(upstream_url, "passthrough")
    try:
        response = httpx.post(
            f"{sidecar_url}/v1/chat/completions",
            json={"model": "usage-stream", "stream": True, "messages": []},
            timeout=10.0,
        )
        assert response.status_code == 200
        assert "data: [DONE]" in response.text
        stats = httpx.get(f"{sidecar_url}/fabric/stats", timeout=10.0).json()
        assert stats["upstream_prompt_tokens"] == 5
    finally:
        _stop(sidecar, upstream)


def test_stream_death_midway_writes_abort_marker_and_counts_error() -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url = _start_sidecar(upstream_url, "passthrough")
    try:
        response = httpx.post(
            f"{sidecar_url}/v1/chat/completions",
            json={"model": "dying", "stream": True, "messages": []},
            timeout=10.0,
        )
        assert response.status_code == 200
        assert '"hi"' in response.text
        assert ": fabric: upstream stream aborted" in response.text
        assert "[DONE]" not in response.text
        stats = httpx.get(f"{sidecar_url}/fabric/stats", timeout=10.0).json()
        assert stats["errors"] == 1
    finally:
        _stop(sidecar, upstream)


def test_upstream_death_before_body_returns_502_not_hang() -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url = _start_sidecar(upstream_url, "passthrough")
    try:
        response = httpx.post(
            f"{sidecar_url}/v1/chat/completions",
            json={"model": "dying-json", "messages": []},
            timeout=10.0,
        )
        assert response.status_code == 502
        assert "upstream unreachable" in response.json()["error"]["message"]
    finally:
        _stop(sidecar, upstream)


def test_missing_content_length_returns_411() -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, _ = _start_sidecar(upstream_url, "passthrough")
    try:
        with socket.create_connection(("127.0.0.1", sidecar.server_address[1]), timeout=10.0) as s:
            s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n\r\n{}")
            data = _read_http_response(s)
        assert b" 411 " in data.split(b"\r\n", 1)[0]
        assert b"Connection: close" in data
    finally:
        _stop(sidecar, upstream)


def test_chunked_request_returns_411() -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, _ = _start_sidecar(upstream_url, "passthrough")
    try:
        with socket.create_connection(("127.0.0.1", sidecar.server_address[1]), timeout=10.0) as s:
            s.sendall(
                b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n"
            )
            data = _read_http_response(s)
        assert b" 411 " in data.split(b"\r\n", 1)[0]
        assert b"Connection: close" in data
    finally:
        _stop(sidecar, upstream)


def test_duplicate_content_length_returns_400() -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, _ = _start_sidecar(upstream_url, "passthrough")
    try:
        with socket.create_connection(("127.0.0.1", sidecar.server_address[1]), timeout=10.0) as s:
            s.sendall(
                b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}"
            )
            data = _read_http_response(s)
        assert b" 400 " in data.split(b"\r\n", 1)[0]
        assert b"Connection: close" in data
    finally:
        _stop(sidecar, upstream)


def test_413_closes_connection_instead_of_desyncing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CF_MAX_BODY_BYTES", "16")
    upstream, upstream_url = _start_upstream()
    sidecar, _ = _start_sidecar(upstream_url, "passthrough")
    try:
        with socket.create_connection(("127.0.0.1", sidecar.server_address[1]), timeout=10.0) as s:
            s.sendall(
                b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n"
                + b"x" * 100
            )
            data = _read_http_response(s)
            assert b" 413 " in data.split(b"\r\n", 1)[0]
            assert b"Connection: close" in data
            try:
                rest = s.recv(4096)
            except (ConnectionResetError, ConnectionAbortedError):
                rest = b""
        assert rest == b""
    finally:
        _stop(sidecar, upstream)


def test_http11_keepalive_relayed_chat_then_health() -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, _ = _start_sidecar(upstream_url, "passthrough")
    try:
        body = b'{"model": "test", "messages": [{"role": "user", "content": "hi"}]}'
        with socket.create_connection(("127.0.0.1", sidecar.server_address[1]), timeout=10.0) as s:
            s.sendall(
                b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            first = _read_http_response(s)
            s.sendall(b"GET /fabric/health HTTP/1.1\r\nHost: x\r\n\r\n")
            second = _read_http_response(s)
        assert b" 200 " in first.split(b"\r\n", 1)[0]
        assert b" 200 " in second.split(b"\r\n", 1)[0]
    finally:
        _stop(sidecar, upstream)


def test_cors_on_stdlib_error_path() -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url = _start_sidecar(upstream_url, "passthrough")
    try:
        # PUT hits stdlib send_error() (no do_PUT). Deliberately no request body: the
        # 501 path closes without reading a body, and on Windows closing with unread
        # inbound bytes is an abortive close (RST) that can race the response off the
        # wire — reproduced 30/30 when the body arrives as a separate segment. No body
        # means nothing unread, so the graceful close (and the response) is reliable.
        response = httpx.request("PUT", f"{sidecar_url}/fabric/health", timeout=10.0)
        assert response.status_code == 501
        assert response.headers["access-control-allow-origin"] == "*"
        assert response.headers["content-length"]
    finally:
        _stop(sidecar, upstream)


def test_derived_fields_are_server_computed(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url, db = _writable_sidecar(tmp_path, upstream_url, mode="passthrough")
    try:
        response = _post_block(
            sidecar_url,
            {
                "block_kind": "fact",
                "content": "server computes the rest",
                "trusted": True,
                "content_hash": "sha256:evil",
                "token_estimate": 999999,
                "source_event_seq": 99,
                "lifecycle_status": "draft",
                "memory_tier": 0,
                "fidelity": "raw",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["content_hash"] != "sha256:evil"
        assert data["lifecycle_status"] == "routable"
        assert data["source_event_seq"] == 1
        (stored,) = SqliteStore(db).load_blocks()
        assert stored.content_hash == data["content_hash"]
        assert stored.token_estimate != 999999
        assert stored.lifecycle_status.value == "routable"
    finally:
        _stop(sidecar, upstream)


def test_post_block_rejects_empty_text_and_bad_block_ids(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url, _ = _writable_sidecar(tmp_path, upstream_url, mode="passthrough")
    try:
        assert _post_block(sidecar_url, {"block_kind": "fact", "content": ""}).status_code == 400
        assert _post_block(sidecar_url, {"block_kind": "fact", "ref_uri": "  "}).status_code == 400
        assert (
            _post_block(
                sidecar_url, {"block_kind": "fact", "content": "x", "block_id": "a\nb"}
            ).status_code
            == 400
        )
        assert (
            _post_block(
                sidecar_url, {"block_kind": "fact", "content": "x", "block_id": "a b"}
            ).status_code
            == 400
        )
        assert (
            _post_block(
                sidecar_url, {"block_kind": "fact", "content": "x", "block_id": "x" * 129}
            ).status_code
            == 400
        )
    finally:
        _stop(sidecar, upstream)


def test_trusted_repost_of_draft_reports_stored_lifecycle(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url, db = _writable_sidecar(tmp_path, upstream_url, mode="passthrough")
    try:
        payload = {"block_kind": "fact", "content": "cannot be promoted by re-post"}
        assert _post_block(sidecar_url, payload).status_code == 201
        again = _post_block(sidecar_url, {**payload, "trusted": True})
        assert again.status_code == 200
        assert again.json()["deduplicated"] is True
        assert again.json()["lifecycle_status"] == "draft"
        assert SqliteStore(db).load_blocks()[0].lifecycle_status.value == "draft"
    finally:
        _stop(sidecar, upstream)


def test_concurrent_posts_allocate_unique_event_seqs(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url, db = _writable_sidecar(tmp_path, upstream_url, mode="passthrough")
    try:

        def post(i: int) -> httpx.Response:
            return _post_block(sidecar_url, {"block_kind": "fact", "content": f"concurrent-{i}"})

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(post, range(24)))
        assert [r.status_code for r in responses] == [201] * 24
        seqs = [r.json()["source_event_seq"] for r in responses]
        assert len(set(seqs)) == 24
        assert SqliteStore(db).count() == 24
    finally:
        _stop(sidecar, upstream)


def test_concurrent_duplicate_posts_insert_once(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream()
    sidecar, sidecar_url, db = _writable_sidecar(tmp_path, upstream_url, mode="passthrough")
    try:
        payload = {"block_kind": "fact", "content": "raced duplicate"}

        def post(_: int) -> httpx.Response:
            return _post_block(sidecar_url, payload)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(post, range(16)))
        codes = [r.status_code for r in responses]
        assert codes.count(201) == 1
        assert set(codes) <= {200, 201}
        assert SqliteStore(db).count() == 1
    finally:
        _stop(sidecar, upstream)
