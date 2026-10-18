"""``cfx fold`` / ``cfx fork`` CLI tests, plus the repeat-``packet_id`` receipt warning."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from kernel.cartridge.serializer import count_tokens
from kernel.mmu.epoch_folder import fold_raw_events
from kernel.router.compiler import compile_packet
from kernel.store import CompileManifest, SqliteStore
from protocol.canonical import sha256_hex
from protocol.models import (
    BlockKind,
    ContextBlock,
    Creator,
    CreatorType,
    Fidelity,
    LifecycleStatus,
    MemoryTier,
    Recipient,
    Scope,
)
from sidecar.cli import main as cfx_main
from sidecar.server import make_server, open_store

SCOPE = Scope(tenant_id="default", project_id="default", workflow_id="chat", task_id="chat")
RECIPIENT = Recipient(agent_id="a1", role="user", stage="chat")
AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
LONG = " ".join(f"tok{i}" for i in range(120))

FORK_ARGV = ["--agent", "aud", "--role", "auditor", "--objective", "audit storage"]


def raw_block(
    bid: str,
    seq: int,
    content: str,
    *,
    tokens: int = 100,
    kind: BlockKind = BlockKind.observation,
    fidelity: Fidelity = Fidelity.raw,
    lifecycle: LifecycleStatus = LifecycleStatus.routable,
) -> ContextBlock:
    return ContextBlock(
        block_id=bid,
        block_kind=kind,
        memory_tier=MemoryTier.register,
        content=content,
        content_hash="sha256:" + sha256_hex(content.encode("utf-8")),
        token_estimate=tokens,
        source_event_seq=seq,
        creator=Creator(type=CreatorType.tool, id="bash"),
        fidelity=fidelity,
        lifecycle_status=lifecycle,
        trust_domain="default",
        observed_at=AS_OF,
        valid_from=AS_OF,
    )


def decision(bid: str, seq: int, content: str) -> ContextBlock:
    return ContextBlock(
        block_id=bid,
        block_kind=BlockKind.decision,
        memory_tier=MemoryTier.ledger,
        content=content,
        content_hash="sha256:" + sha256_hex(content.encode("utf-8")),
        token_estimate=count_tokens(content),
        source_event_seq=seq,
        creator=Creator(type=CreatorType.human, id="tester"),
        fidelity=Fidelity.extract,
        trust_domain="default",
        observed_at=AS_OF,
        valid_from=AS_OF,
    )


def compile_and_log(store: SqliteStore, objective: str = "hello fabric") -> CompileManifest:
    """A real compile + manifest log, exactly like the sidecar's compile path."""
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
    pairs = sorted({b.block_id: b.content_hash for b in blocks}.items())
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


# ---------------------------------------------------------------- cfx fold


def test_fold_appends_facts_and_keeps_raw_events(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "fabric.db"
    store = SqliteStore(db)
    events = [raw_block(f"obs_{i}", i, LONG) for i in (1, 2, 3)]
    for event in events:
        store.append(event)

    assert cfx_main(["fold", "--store", str(db)]) == 0
    out = capsys.readouterr().out
    assert "folded: 3" in out
    assert "appended: 3" in out
    assert "deduped: 0" in out

    expected = fold_raw_events(events, k=3)
    facts = [b for b in expected.ledger if b.block_kind is BlockKind.fact]
    assert len(facts) == 3
    stored = SqliteStore(db).load_blocks()
    assert len(stored) == 6, "raw events stay untouched; facts are appended"
    for fact in facts:
        assert fact in stored
    for event in events:
        assert event in stored


def test_fold_is_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "fabric.db"
    store = SqliteStore(db)
    for i in (1, 2, 3):
        store.append(raw_block(f"obs_{i}", i, LONG))

    assert cfx_main(["fold", "--store", str(db)]) == 0
    assert SqliteStore(db).count() == 6
    capsys.readouterr()
    assert cfx_main(["fold", "--store", str(db)]) == 0
    out = capsys.readouterr().out
    assert "folded: 3" in out
    assert "appended: 0" in out
    assert "deduped: 3" in out
    assert SqliteStore(db).count() == 6, "re-fold must not duplicate facts"


def test_fold_selection_rule_skips_non_raw_events(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "fabric.db"
    store = SqliteStore(db)
    store.append(raw_block("obs_extract", 1, LONG, fidelity=Fidelity.extract))
    store.append(raw_block("obs_draft", 2, LONG, lifecycle=LifecycleStatus.draft))
    store.append(raw_block("dec_raw", 3, LONG, kind=BlockKind.decision))
    store.append(
        ContextBlock(
            block_id="art_ref",
            block_kind=BlockKind.artifact,
            memory_tier=MemoryTier.register,
            ref_uri="ctx://object/big",
            content_hash="sha256:x",
            token_estimate=999,
            source_event_seq=4,
            creator=Creator(type=CreatorType.tool, id="bash"),
            fidelity=Fidelity.raw,
            trust_domain="default",
            observed_at=AS_OF,
            valid_from=AS_OF,
        )
    )

    assert cfx_main(["fold", "--store", str(db)]) == 0
    out = capsys.readouterr().out
    assert "folded: 0" in out and "appended: 0" in out and "deduped: 0" in out
    assert SqliteStore(db).count() == 4


def test_fold_k_gate_keeps_incomplete_tail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "fabric.db"
    store = SqliteStore(db)
    for i in (1, 2, 3, 4):
        store.append(raw_block(f"obs_{i}", i, LONG))

    assert cfx_main(["fold", "--store", str(db)]) == 0
    out = capsys.readouterr().out
    assert "folded: 3" in out and "appended: 3" in out
    assert SqliteStore(db).count() == 7, "the 4th event stays raw (k-step gate)"

    assert cfx_main(["fold", "--store", str(db), "--k", "1"]) == 0
    out = capsys.readouterr().out
    assert "folded: 4" in out
    assert "appended: 1" in out, "only the tail event is new at k=1"
    assert "deduped: 3" in out


def test_fold_k_zero_is_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "fabric.db"
    SqliteStore(db)
    assert cfx_main(["fold", "--store", str(db), "--k", "0"]) == 2
    assert "--k must be >= 1" in capsys.readouterr().err


def test_fold_store_errors_exit_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cfx_main(["fold", "--store", str(tmp_path / "missing.db")]) == 1
    assert "store not found" in capsys.readouterr().err

    bad = tmp_path / "fake.db"
    bad.write_text("not a database", encoding="utf-8")
    assert cfx_main(["fold", "--store", str(bad)]) == 1
    assert "cannot open store" in capsys.readouterr().err


# ---------------------------------------------------------------- cfx fork


def test_fork_emits_child_packet(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "fabric.db"
    store = SqliteStore(db)
    store.append(decision("d1", 1, "Use Postgres for canonical storage"))
    manifest = compile_and_log(store)

    argv = ["fork", "--store", str(db), "--packet-id", manifest.packet_id, *FORK_ARGV]
    assert cfx_main([*argv, "--stage", "audit"]) == 0
    captured = capsys.readouterr()
    child = json.loads(captured.out)

    assert child["parent_packet_id"] == manifest.packet_id
    assert child["recipient"] == {"agent_id": "aud", "role": "auditor", "stage": "audit"}
    assert child["epoch_number"] == 2 and child["spawn_depth"] == 1
    assert [b["block_id"] for b in child["tiers"]["tier1"]["blocks"]] == ["d1"]
    assert child["tiers"]["tier1"]["blocks"][0]["content_hash"] == (
        "sha256:" + sha256_hex(b"Use Postgres for canonical storage")
    )
    assert child["tiers"]["tier3"]["observations"] == []
    assert child["tiers"]["tier3"]["instruction"]["content"] == (
        "OBJECTIVE: audit storage\nSTAGE: audit\nROLE: auditor"
    )
    assert f"child_packet_id: {child['packet_id']}" in captured.err
    assert f"parent_packet_id: {manifest.packet_id}" in captured.err


def test_fork_stage_defaults_to_parent_stage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "fabric.db"
    store = SqliteStore(db)
    store.append(decision("d1", 1, "Use Postgres for canonical storage"))
    manifest = compile_and_log(store)
    argv = ["fork", "--store", str(db), "--packet-id", manifest.packet_id, *FORK_ARGV]
    assert cfx_main(argv) == 0
    child = json.loads(capsys.readouterr().out)
    assert child["recipient"]["stage"] == "chat"
    assert child["tiers"]["tier3"]["instruction"]["content"].endswith("STAGE: chat\nROLE: auditor")


def test_fork_is_deterministic_and_out_matches_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "fabric.db"
    store = SqliteStore(db)
    store.append(decision("d1", 1, "Use Postgres for canonical storage"))
    manifest = compile_and_log(store)
    argv = ["fork", "--store", str(db), "--packet-id", manifest.packet_id, *FORK_ARGV]
    out_file = tmp_path / "child.json"

    assert cfx_main([*argv, "--stage", "audit"]) == 0
    first = capsys.readouterr().out
    assert cfx_main([*argv, "--stage", "audit", "--out", str(out_file)]) == 0
    second = capsys.readouterr().out
    assert first == second, "identical invocations must emit byte-identical JSON"
    assert out_file.read_text(encoding="utf-8").rstrip("\n") == second.rstrip("\n")


def test_fork_drift_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "fabric.db"
    store = SqliteStore(db)
    store.append(decision("d1", 1, "Use Postgres for canonical storage"))
    manifest = compile_and_log(store)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM blocks WHERE block_id = 'd1'")
    conn.commit()
    conn.close()

    argv = ["fork", "--store", str(db), "--packet-id", manifest.packet_id, *FORK_ARGV]
    assert cfx_main(argv) == 1
    assert "store drift: block d1 changed/missing" in capsys.readouterr().err


def test_fork_unknown_packet_and_missing_store_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "fabric.db"
    SqliteStore(db)
    argv = ["fork", "--store", str(db), "--packet-id", "cpk_missing", *FORK_ARGV]
    assert cfx_main(argv) == 1
    assert "no compile manifest" in capsys.readouterr().err

    missing = ["fork", "--store", str(tmp_path / "missing.db"), "--packet-id", "cpk_x", *FORK_ARGV]
    assert cfx_main(missing) == 1
    assert "store not found" in capsys.readouterr().err


def test_fork_empty_arguments_are_usage_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "fabric.db"
    SqliteStore(db)
    argv = ["fork", "--store", str(db), "--packet-id", "cpk_x", "--agent", "a", "--role", ""]
    argv += ["--objective", "audit"]
    assert cfx_main(argv) == 2
    assert "must be non-empty" in capsys.readouterr().err


# ------------------------------------------- repeat packet_id receipt warning


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


def test_repeat_packet_id_warns_and_first_receipt_wins(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "fabric.db"
    store = open_store(db)
    store.append(decision("d1", 1, "Use Postgres for canonical storage"))
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

    def chat() -> httpx.Response:
        return httpx.post(
            f"{sidecar_url}/v1/chat/completions",
            json={"model": "test", "messages": [{"role": "user", "content": "hello fabric"}]},
            timeout=10.0,
        )

    try:
        first = chat()
        assert first.status_code == 200
        packet_id = first.headers["x-fabric-packet-id"]
        first_receipt = store.get_compile(packet_id)
        assert first_receipt is not None

        second = chat()
        assert second.status_code == 200
        assert second.headers["x-fabric-packet-id"] == packet_id
        err = capsys.readouterr().err
        assert "already receipted" in err
        assert "first receipt wins" in err

        (manifest,) = SqliteStore(db).load_compile_log()
        assert manifest.packet_id == packet_id
        assert manifest.content_hash == first_receipt.content_hash
    finally:
        for server in (sidecar, upstream):
            server.shutdown()
            server.server_close()
