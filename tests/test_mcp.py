"""MCP stdio server tests: handshake, tools/list, quarantine, receipts, readonly, errors.

Each test spawns ``python -m sidecar.mcp`` as a subprocess and speaks newline-delimited
JSON-RPC 2.0 over its stdin/stdout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kernel.router.compiler import compile_packet
from kernel.store import SqliteStore
from protocol.epoch import create_epoch, merkle_root
from protocol.models import Recipient, Scope
from protocol.signing import Signer
from sidecar.blocks import build_block

ROOT = Path(__file__).resolve().parents[1]
SIGNER = Signer.from_seed_hex("00" * 32)
AS_OF = datetime(2026, 1, 1, tzinfo=UTC)


@contextmanager
def _server(
    db: Path, *extra: str, env: dict[str, str] | None = None
) -> Iterator[subprocess.Popen[str]]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "sidecar.mcp", "--store", str(db), *extra],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env={**os.environ, **(env or {})},
    )
    try:
        yield proc
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise


def _send(proc: subprocess.Popen[str], message: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def _recv(proc: subprocess.Popen[str]) -> dict[str, Any]:
    assert proc.stdout is not None
    line = proc.stdout.readline()
    assert line, "server closed stdout unexpectedly"
    return json.loads(line)


def _rpc(
    proc: subprocess.Popen[str],
    method: str,
    params: dict[str, Any] | None = None,
    request_id: int = 1,
) -> dict[str, Any]:
    _send(proc, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
    return _recv(proc)


def _handshake(proc: subprocess.Popen[str]) -> None:
    result = _rpc(
        proc,
        "initialize",
        {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {}},
    )
    assert result["result"]["protocolVersion"] == "2025-11-25"
    _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})


def _call(
    proc: subprocess.Popen[str], name: str, arguments: dict[str, Any], request_id: int = 2
) -> dict[str, Any]:
    return _rpc(proc, "tools/call", {"name": name, "arguments": arguments}, request_id)


def _text(response: dict[str, Any]) -> dict[str, Any]:
    return json.loads(response["result"]["content"][0]["text"])


def test_initialize_negotiates_and_ignores_notifications(tmp_path: Path) -> None:
    with _server(tmp_path / "fabric.db") as proc:
        result = _rpc(
            proc,
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}},
        )
        assert result["result"]["protocolVersion"] == "2025-06-18"
        assert result["result"]["capabilities"] == {"tools": {}}
        assert result["result"]["serverInfo"]["name"] == "context-fabric"
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/unknown/thing"})
        pong = _rpc(proc, "ping", request_id=7)
        assert pong == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_tools_list_schemas(tmp_path: Path) -> None:
    with _server(tmp_path / "fabric.db") as proc:
        _handshake(proc)
        result = _rpc(proc, "tools/list", request_id=8)
        tools = {tool["name"]: tool for tool in result["result"]["tools"]}
        assert set(tools) == {"memory_search", "receipt_verify", "memory_add", "expand"}
        for tool in tools.values():
            assert tool["inputSchema"]["type"] == "object"
        assert tools["memory_search"]["inputSchema"]["required"] == ["query"]
        assert tools["memory_add"]["inputSchema"]["required"] == ["block_kind", "content"]


def test_memory_search_excludes_quarantined_by_default(tmp_path: Path) -> None:
    db = tmp_path / "fabric.db"
    store = SqliteStore(db)
    store.append(
        build_block(
            {"block_kind": "fact", "content": "Use Postgres for canonical storage"},
            trusted=True,
            event_seq=1,
        )
    )
    store.append(
        build_block(
            {"block_kind": "fact", "content": "Postgres is only a draft note here"},
            trusted=False,
            event_seq=2,
        )
    )
    with _server(db) as proc:
        _handshake(proc)
        default = _text(_call(proc, "memory_search", {"query": "postgres"}))
        assert [r["lifecycle_status"] for r in default["results"]] == ["routable"]
        assert default["results"][0]["block_id"].startswith("blk_")
        assert default["results"][0]["block_kind"] == "fact"
        assert default["results"][0]["content_excerpt"] == "Use Postgres for canonical storage"
        with_flag = _text(
            _call(
                proc,
                "memory_search",
                {"query": "postgres", "include_quarantined": True},
                request_id=3,
            )
        )
        assert sorted(r["lifecycle_status"] for r in with_flag["results"]) == [
            "draft",
            "routable",
        ]


def test_receipt_verify_ok_and_tampered(tmp_path: Path) -> None:
    packet = compile_packet(
        scope=Scope(tenant_id="t1", project_id="p1", workflow_id="w1", task_id="task1"),
        recipient=Recipient(agent_id="a1", role="implementer", stage="implement"),
        objective="alpha",
        stage="implement",
        blocks=[],
        as_of=AS_OF,
        signer=SIGNER,
    ).packet
    epoch = create_epoch(
        tenant_id="t1",
        watermark_event_seq=1,
        packet_hashes=(packet.hashes.content_hash,),
        created_at=AS_OF,
        signer=SIGNER,
    )
    good = json.dumps([epoch.model_dump(mode="json")])
    tampered = json.dumps([{**epoch.model_dump(mode="json"), "tenant_id": "t2"}])
    with _server(tmp_path / "fabric.db", env={"CF_RECEIPT_PUBKEY": SIGNER.public_hex}) as proc:
        _handshake(proc)
        ok = _text(_call(proc, "receipt_verify", {"epochs_json": good}))
        assert ok["ok"] is True
        assert ok["epochs_verified"] == 1
        bad = _text(_call(proc, "receipt_verify", {"epochs_json": tampered}, request_id=3))
        assert bad["ok"] is False
        assert "failed" in bad["reason"]


def test_receipt_verify_lone_surrogate_is_rejected_not_fatal(tmp_path: Path) -> None:
    # A lone surrogate cannot be canonicalized to UTF-8; it must be -32602, not a crash.
    hashes = ("sha256:abc",)
    epoch = {
        "epoch_id": "epc_x",
        "created_at": "2026-01-01T00:00:00Z",
        "tenant_id": "\ud800",
        "watermark_event_seq": 0,
        "prev_epoch_hash": None,
        "packet_hashes": list(hashes),
        "merkle_root": merkle_root(hashes),
        "packet_count": 1,
        "key_id": SIGNER.key_id,
        "signature": "00" * 64,
    }
    with _server(tmp_path / "fabric.db", env={"CF_RECEIPT_PUBKEY": SIGNER.public_hex}) as proc:
        _handshake(proc)
        bad = _call(proc, "receipt_verify", {"epochs_json": json.dumps([epoch])})
        assert bad["error"]["code"] == -32602
        assert "invalid epoch" in bad["error"]["message"]
        pong = _rpc(proc, "ping", request_id=3)
        assert pong["result"] == {}


def test_memory_add_draft_and_readonly_rejects(tmp_path: Path) -> None:
    db = tmp_path / "fabric.db"
    with _server(db) as proc:
        _handshake(proc)
        added = _text(
            _call(proc, "memory_add", {"block_kind": "decision", "content": "Adopt receipts"})
        )
        assert added["block_id"].startswith("blk_")
        assert added["lifecycle_status"] == "draft"
        trusted = _text(
            _call(
                proc,
                "memory_add",
                {"block_kind": "decision", "content": "Adopt receipts v2", "trusted": True},
                request_id=3,
            )
        )
        assert trusted["lifecycle_status"] == "routable"
    assert SqliteStore(db).count() == 2
    with _server(db, "--readonly") as proc:
        _handshake(proc)
        refused = _call(proc, "memory_add", {"block_kind": "fact", "content": "nope"})
        assert refused["error"]["code"] == -32001
        assert "readonly" in refused["error"]["message"]
    assert SqliteStore(db).count() == 2


def test_unknown_tool_and_bad_params(tmp_path: Path) -> None:
    with _server(tmp_path / "fabric.db") as proc:
        _handshake(proc)
        unknown = _call(proc, "nope", {})
        assert unknown["error"]["code"] == -32602
        assert "Unknown tool" in unknown["error"]["message"]
        missing = _call(proc, "memory_search", {}, request_id=3)
        assert missing["error"]["code"] == -32602
        assert "query" in missing["error"]["message"]


def test_expand_tool_resolves_folded_observation(tmp_path: Path) -> None:
    from sidecar.obsfold import fold_messages

    db = tmp_path / "obs.db"
    store = SqliteStore(db)
    content = "def fix():\n    return 42\n"
    messages = [
        {"role": "user", "content": "read the file"},
        {"role": "assistant", "content": "calling"},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": content},
    ]
    result = fold_messages(messages, n=0, k=1, store=store, created_at=AS_OF)
    assert result.folded == 1
    uri = "ctx://obs/" + result.events[0].stub_id
    with _server(db) as proc:
        _handshake(proc)
        found = _text(_call(proc, "expand", {"uri": uri}))
        assert found["found"] is True and found["content"] == content
        missing = _text(_call(proc, "expand", {"uri": "ctx://obs/deadbeefdead"}, request_id=3))
        assert missing["found"] is False


def test_malformed_json_line_recovers(tmp_path: Path) -> None:
    with _server(tmp_path / "fabric.db") as proc:
        assert proc.stdin is not None
        proc.stdin.write("this is not json\n")
        proc.stdin.flush()
        broken = _recv(proc)
        assert broken["error"]["code"] == -32700
        assert broken["id"] is None
        pong = _rpc(proc, "ping", request_id=9)
        assert pong["result"] == {}


def test_malformed_utf8_line_recovers(tmp_path: Path) -> None:
    with _server(tmp_path / "fabric.db") as proc:
        assert proc.stdin is not None
        proc.stdin.buffer.write(b'\xff\xfe{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
        proc.stdin.buffer.flush()
        broken = _recv(proc)
        assert broken["error"]["code"] == -32700
        pong = _rpc(proc, "ping", request_id=10)
        assert pong["result"] == {}


def _signed_epochs_json(signer: Signer = SIGNER) -> str:
    packet = compile_packet(
        scope=Scope(tenant_id="t1", project_id="p1", workflow_id="w1", task_id="task1"),
        recipient=Recipient(agent_id="a1", role="implementer", stage="implement"),
        objective="alpha",
        stage="implement",
        blocks=[],
        as_of=AS_OF,
        signer=signer,
    ).packet
    epoch = create_epoch(
        tenant_id="t1",
        watermark_event_seq=1,
        packet_hashes=(packet.hashes.content_hash,),
        created_at=AS_OF,
        signer=signer,
    )
    return json.dumps([epoch.model_dump(mode="json")])


def test_receipt_verify_missing_and_invalid_pubkey(tmp_path: Path) -> None:
    good = _signed_epochs_json()
    with _server(tmp_path / "fabric.db") as proc:
        _handshake(proc)
        missing = _text(_call(proc, "receipt_verify", {"epochs_json": good}))
        assert missing["ok"] is False
        assert "not set" in missing["reason"]
    with _server(tmp_path / "fabric.db", env={"CF_RECEIPT_PUBKEY": "zz"}) as proc:
        _handshake(proc)
        bad = _text(_call(proc, "receipt_verify", {"epochs_json": good}))
        assert bad["ok"] is False
        assert "hex" in bad["reason"]


def test_receipt_verify_key_id_mismatch(tmp_path: Path) -> None:
    other = Signer.from_seed_hex("11" * 32)
    good = _signed_epochs_json()
    with _server(tmp_path / "fabric.db", env={"CF_RECEIPT_PUBKEY": other.public_hex}) as proc:
        _handshake(proc)
        bad = _text(_call(proc, "receipt_verify", {"epochs_json": good}))
        assert bad["ok"] is False
        assert "key_id" in bad["reason"]


def test_memory_search_truncates_excerpt_and_respects_limit(tmp_path: Path) -> None:
    db = tmp_path / "fabric.db"
    store = SqliteStore(db)
    store.append(
        build_block({"block_kind": "fact", "content": "alpha " * 300}, trusted=True, event_seq=1)
    )
    store.append(
        build_block({"block_kind": "fact", "content": "alpha beta"}, trusted=True, event_seq=2)
    )
    with _server(db) as proc:
        _handshake(proc)
        result = _text(_call(proc, "memory_search", {"query": "alpha", "limit": 1}))
        assert len(result["results"]) == 1
        assert len(result["results"][0]["content_excerpt"]) == 300
        assert result["results"][0]["content_excerpt"].endswith("...")


def test_initialize_requires_string_protocol_version(tmp_path: Path) -> None:
    with _server(tmp_path / "fabric.db") as proc:
        bad = _rpc(proc, "initialize", {})
        assert bad["error"]["code"] == -32602
        assert "protocolVersion" in bad["error"]["message"]
        pong = _rpc(proc, "ping", request_id=3)
        assert pong["result"] == {}
