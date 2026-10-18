"""``cfx replay`` / ``cfx seal`` tests — manifest roundtrip, drift, epoch chain, sidecar E2E."""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from kernel.router.compiler import compile_packet
from kernel.store import CompileManifest, SqliteStore
from protocol import epoch
from protocol.canonical import canonicalize
from protocol.models import (
    BlockKind,
    ContextBlock,
    Creator,
    CreatorType,
    Fidelity,
    MemoryTier,
    Recipient,
    Scope,
)
from protocol.signing import Signer
from sidecar import server as server_module
from sidecar.cli import main as cfx_main
from sidecar.replay import replay, seal
from sidecar.server import make_server, open_store

SCOPE = Scope(tenant_id="default", project_id="default", workflow_id="chat", task_id="chat")
RECIPIENT = Recipient(agent_id="a1", role="user", stage="chat")
AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
KEY_HEX = "42" * 32
PUB_HEX = Signer.from_seed_hex(KEY_HEX).public_hex


def block(bid: str, seq: int, content: str, *, content_hash: str | None = None) -> ContextBlock:
    return ContextBlock(
        block_id=bid,
        block_kind=BlockKind.decision,
        memory_tier=MemoryTier.ledger,
        content=content,
        content_hash=content_hash or f"sha256:{bid}",
        token_estimate=8,
        source_event_seq=seq,
        creator=Creator(type=CreatorType.human, id="tester"),
        fidelity=Fidelity.extract,
        trust_domain="default",
        observed_at=AS_OF,
        valid_from=AS_OF,
    )


def compile_and_log(store: SqliteStore, objective: str = "hello fabric") -> CompileManifest:
    blocks = store.load_blocks()
    packet = compile_packet(
        scope=SCOPE,
        recipient=RECIPIENT,
        objective=objective,
        stage="chat",
        blocks=blocks,
        watermark_event_seq=0,
        budget_base=4000,
        as_of=AS_OF,
    ).packet
    pairs = sorted((b.block_id, b.content_hash) for b in blocks)
    manifest = CompileManifest(
        packet_id=packet.packet_id,
        content_hash=packet.hashes.content_hash,
        scope=SCOPE,
        recipient=RECIPIENT,
        objective=objective,
        stage="chat",
        as_of=AS_OF,
        watermark_event_seq=0,
        budget_base=4000,
        block_ids=tuple(block_id for block_id, _ in pairs),
        block_hashes=tuple(content_hash for _, content_hash in pairs),
        model="test-model",
        engine="test-engine",
        purpose=None,
        created_at=AS_OF,
    )
    store.log_compile(manifest)
    return manifest


def _tamper(db: Path, replacement: ContextBlock) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE blocks SET content_hash = ?, payload = ? WHERE block_id = ?",
        (
            replacement.content_hash,
            canonicalize(replacement.model_dump(mode="json")).decode("utf-8"),
            replacement.block_id,
        ),
    )
    conn.commit()
    conn.close()


def test_replay_ok_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "fabric.db"
    store = SqliteStore(db)
    store.append(block("d1", 1, "Use Postgres for canonical storage"))
    store.append(block("d2", 2, "Ship the sidecar before the dashboard"))
    manifest = compile_and_log(store)
    assert replay(db, manifest.packet_id) == 0
    out = capsys.readouterr().out
    assert f"REPLAY OK {manifest.packet_id} {manifest.content_hash}" in out


def test_replay_insensitive_to_new_blocks(tmp_path: Path) -> None:
    db = tmp_path / "fabric.db"
    store = SqliteStore(db)
    store.append(block("d1", 1, "Use Postgres for canonical storage"))
    manifest = compile_and_log(store)
    store.append(block("d3", 3, "A note added after the compile"))
    assert replay(db, manifest.packet_id) == 0


def test_replay_detects_tampered_block(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "fabric.db"
    store = SqliteStore(db)
    store.append(block("d1", 1, "Use Postgres for canonical storage"))
    store.append(block("d2", 2, "Ship the sidecar"))
    manifest = compile_and_log(store)
    _tamper(db, block("d1", 1, "MALICIOUS", content_hash="sha256:evil"))
    assert replay(db, manifest.packet_id) == 1
    assert "store drift: block d1 changed/missing" in capsys.readouterr().err


def test_replay_detects_removed_block(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "fabric.db"
    store = SqliteStore(db)
    store.append(block("d1", 1, "Use Postgres for canonical storage"))
    store.append(block("d2", 2, "Ship the sidecar"))
    manifest = compile_and_log(store)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM blocks WHERE block_id = 'd2'")
    conn.commit()
    conn.close()
    assert replay(db, manifest.packet_id) == 1
    assert "store drift: block d2 changed/missing" in capsys.readouterr().err


def test_replay_hash_mismatch_reports_both_hashes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "fabric.db"
    store = SqliteStore(db)
    store.append(block("d1", 1, "Use Postgres for canonical storage"))
    manifest = compile_and_log(store)
    lying = CompileManifest(
        packet_id=manifest.packet_id,
        content_hash="sha256:bogus",
        scope=manifest.scope,
        recipient=manifest.recipient,
        objective=manifest.objective,
        stage=manifest.stage,
        as_of=manifest.as_of,
        watermark_event_seq=manifest.watermark_event_seq,
        budget_base=manifest.budget_base,
        block_ids=manifest.block_ids,
        block_hashes=manifest.block_hashes,
        model=manifest.model,
        engine=manifest.engine,
        purpose=manifest.purpose,
        created_at=manifest.created_at,
    )
    (db.parent / "lying.db").unlink(missing_ok=True)
    lying_store = SqliteStore(db.parent / "lying.db")
    lying_store.append(block("d1", 1, "Use Postgres for canonical storage"))
    lying_store.log_compile(lying)
    assert replay(db.parent / "lying.db", manifest.packet_id) == 1
    out = capsys.readouterr().out
    assert "REPLAY MISMATCH sha256:bogus" in out
    assert manifest.content_hash in out


def test_replay_unknown_packet_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "fabric.db"
    SqliteStore(db)
    assert replay(db, "cpk_missing") == 1
    assert "no compile manifest" in capsys.readouterr().err


def test_replay_cli_wiring(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "fabric.db"
    store = SqliteStore(db)
    store.append(block("d1", 1, "Use Postgres for canonical storage"))
    manifest = compile_and_log(store)
    assert cfx_main(["replay", "--store", str(db), "--packet-id", manifest.packet_id]) == 0
    assert "REPLAY OK" in capsys.readouterr().out


def test_seal_verify_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "fabric.db"
    store = SqliteStore(db)
    store.append(block("d1", 1, "Use Postgres for canonical storage"))
    compile_and_log(store, "objective one")
    compile_and_log(store, "objective two")
    out = tmp_path / "epochs.json"
    assert seal(db, KEY_HEX, out, model="m", engine="e") == 0
    assert "epoch_id: epc_" in capsys.readouterr().out
    (sealed,) = [epoch.Epoch.model_validate(x) for x in json.loads(out.read_text())]
    assert sealed.packet_count == 2
    assert sealed.watermark_event_seq == 0
    assert sealed.meta is not None
    assert sealed.meta.model_id == "m"
    assert sealed.meta.engine == "e"
    assert sealed.meta.tokenizer_id == "o200k_base"
    assert sealed.meta.note == "output_reproduction_out_of_scope"
    assert epoch.main(["verify", str(out), "--pubkey", PUB_HEX]) == 0


def test_seal_appends_linked_epochs_and_reseal_is_noop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "fabric.db"
    store = SqliteStore(db)
    store.append(block("d1", 1, "Use Postgres for canonical storage"))
    compile_and_log(store, "objective one")
    out = tmp_path / "epochs.json"
    assert seal(db, KEY_HEX, out) == 0
    store.append(block("d2", 2, "Now with a second note"))
    compile_and_log(store, "objective two")
    assert seal(db, KEY_HEX, out) == 0
    epochs = [epoch.Epoch.model_validate(x) for x in json.loads(out.read_text())]
    assert len(epochs) == 2
    assert epochs[1].prev_epoch_hash == epoch.epoch_content_hash(epochs[0])
    assert epoch.main(["verify", str(out), "--pubkey", PUB_HEX]) == 0
    assert seal(db, KEY_HEX, out) == 0
    assert "nothing new" in capsys.readouterr().out
    assert len(json.loads(out.read_text())) == 2


def test_seal_empty_compile_log_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "fabric.db"
    SqliteStore(db)
    assert seal(db, KEY_HEX, tmp_path / "epochs.json") == 1
    assert "compile log is empty" in capsys.readouterr().err


def test_seal_rejects_bad_private_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "fabric.db"
    store = SqliteStore(db)
    store.append(block("d1", 1, "Use Postgres"))
    compile_and_log(store)
    assert seal(db, "not-hex", tmp_path / "epochs.json") == 2
    assert "--private-key" in capsys.readouterr().err


def test_cli_seal_then_verify_wiring(tmp_path: Path) -> None:
    db = tmp_path / "fabric.db"
    store = SqliteStore(db)
    store.append(block("d1", 1, "Use Postgres for canonical storage"))
    compile_and_log(store, "objective one")
    out = tmp_path / "epochs.json"
    argv = [
        "seal",
        "--store",
        str(db),
        "--private-key",
        KEY_HEX,
        "--out",
        str(out),
        "--model",
        "m",
        "--engine",
        "e",
    ]
    assert cfx_main(argv) == 0
    assert cfx_main(["verify", str(out), "--pubkey", PUB_HEX]) == 0


class _FakeUpstream(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def _serve(server: ThreadingHTTPServer) -> str:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


def _stop(*servers: ThreadingHTTPServer) -> None:
    for server in servers:
        server.shutdown()
        server.server_close()


def _chat(base_url: str) -> httpx.Response:
    return httpx.post(
        f"{base_url}/v1/chat/completions",
        json={"model": "test", "messages": [{"role": "user", "content": "hello fabric"}]},
        timeout=10.0,
    )


def test_sidecar_compile_logs_manifest_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CF_MODEL", "fake-model")
    db = tmp_path / "fabric.db"
    store = open_store(db)
    store.append(block("d1", 1, "Use Postgres for canonical storage"))
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
    upstream_url = _serve(upstream)
    sidecar = make_server(
        upstream=upstream_url,
        mode="compile",
        blocks=tuple(store.load_blocks()),
        port=0,
        store=store,
    )
    sidecar_url = _serve(sidecar)
    try:
        response = _chat(sidecar_url)
        assert response.status_code == 200
        assert response.headers["x-fabric-compiled"] == "1"
        (manifest,) = SqliteStore(db).load_compile_log()
        assert manifest.packet_id == response.headers["x-fabric-packet-id"]
        assert manifest.objective == "hello fabric"
        assert manifest.block_ids == ("d1",)
        assert manifest.model == "fake-model"
        assert replay(db, manifest.packet_id) == 0
    finally:
        _stop(sidecar, upstream)


def test_compile_log_failure_does_not_break_proxying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "fabric.db"
    store = open_store(db)
    store.append(block("d1", 1, "Use Postgres for canonical storage"))

    def boom(manifest: CompileManifest) -> None:
        raise RuntimeError("log write exploded")

    monkeypatch.setattr(store, "log_compile", boom)
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
    upstream_url = _serve(upstream)
    sidecar = make_server(
        upstream=upstream_url,
        mode="compile",
        blocks=tuple(store.load_blocks()),
        port=0,
        store=store,
    )
    sidecar_url = _serve(sidecar)
    try:
        response = _chat(sidecar_url)
        assert response.status_code == 200
        assert response.headers["x-fabric-compiled"] == "1"
        assert "compile log write failed" in capsys.readouterr().err
    finally:
        _stop(sidecar, upstream)


def test_manifest_matches_compile_snapshot_under_concurrent_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A block appended after the compile snapshot must not enter that compile's manifest."""
    db = tmp_path / "fabric.db"
    store = open_store(db)
    store.append(block("d1", 1, "Use Postgres for canonical storage"))
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
    upstream_url = _serve(upstream)
    sidecar = make_server(
        upstream=upstream_url,
        mode="compile",
        blocks=tuple(store.load_blocks()),
        port=0,
        store=store,
    )
    sidecar_url = _serve(sidecar)
    entered = threading.Event()
    release = threading.Event()
    real_compile = server_module.compile_packet

    def slow_compile(**kwargs: Any) -> Any:
        result = real_compile(**kwargs)
        entered.set()
        release.wait(5)
        return result

    monkeypatch.setattr(server_module, "compile_packet", slow_compile)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_chat, sidecar_url)
            try:
                assert entered.wait(5), "compile did not reach the pause"
                added = httpx.post(
                    f"{sidecar_url}/fabric/blocks",
                    json={"block_kind": "decision", "content": "late note", "trusted": True},
                    timeout=10.0,
                )
                assert added.status_code == 201
            finally:
                release.set()
            response = future.result(timeout=10)
        assert response.status_code == 200
        packet_id = response.headers["x-fabric-packet-id"]
        manifest = store.get_compile(packet_id)
        assert manifest is not None
        assert manifest.block_ids == ("d1",)
        assert len(SqliteStore(db).load_blocks()) == 2
        assert replay(db, packet_id) == 0
    finally:
        _stop(sidecar, upstream)
