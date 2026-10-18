"""SQLite store tests — lossless roundtrip, append-only semantics, JSONL ingest, CLI."""

from __future__ import annotations

import sqlite3
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kernel.store.__main__ import main
from kernel.store.sqlite_store import SqliteStore, StoreConflict
from protocol.models import (
    BlockKind,
    ContextBlock,
    Creator,
    CreatorType,
    Fidelity,
    LifecycleStatus,
    MemoryTier,
)


def mk(bid: str, seq: int, content: str, *, content_hash: str | None = None) -> ContextBlock:
    return ContextBlock(
        block_id=bid,
        block_kind=BlockKind.decision,
        memory_tier=MemoryTier.ledger,
        content=content,
        content_hash=content_hash or "sha256:" + bid,
        token_estimate=8,
        source_event_seq=seq,
        creator=Creator(type=CreatorType.agent, id="a"),
        fidelity=Fidelity.extract,
        trust_domain="t1",
    )


def test_roundtrip_is_lossless(tmp_path: Path) -> None:
    block = ContextBlock(
        block_id="blk_full",
        block_kind=BlockKind.observation,
        memory_tier=MemoryTier.swap,
        media_type="application/json",
        content=None,
        ref_uri="ctx://object/opaque-1",
        content_hash="sha256:full",
        token_estimate=42,
        tokenizer_name="cl100k_base",
        source_event_seq=7,
        creator=Creator(type=CreatorType.deterministic_probe, id="probe-1"),
        confidence=1.0 / 3.0,
        fidelity=Fidelity.pointer,
        verified=True,
        observed_at=datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC),
        valid_from=datetime(2026, 1, 2, tzinfo=UTC),
        valid_to=datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC),
        lifecycle_status=LifecycleStatus.superseded,
        classification="restricted",
        trust_domain="t1",
        allowed_roles=("builder", "auditor"),
        denied_roles=("intern",),
        allowed_purposes=("debug",),
        symbols_exported=("func_a",),
        symbols_imported=("func_b",),
        counterfactual_meta={"rejected_action": "retry", "blast_radius": 3},
    )
    store = SqliteStore(tmp_path / "store.db")
    assert store.append(block) is True
    assert store.load_blocks() == [block]


def test_reappend_is_deduplicated(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "store.db")
    block = mk("d1", 1, "one")
    assert store.append(block) is True
    assert store.append(block) is False
    assert store.count() == 1


def test_conflicting_content_raises(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "store.db")
    store.append(mk("d1", 1, "one"))
    with pytest.raises(StoreConflict):
        store.append(mk("d1", 1, "two", content_hash="sha256:different"))
    assert store.count() == 1
    assert store.load_blocks()[0].content == "one"


def test_append_many_counts_and_since_seq_filter(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "store.db")
    blocks = [mk("a", 1, "a"), mk("b", 2, "b"), mk("c", 5, "c")]
    assert store.append_many(blocks) == (3, 0)
    assert store.append_many(blocks) == (0, 3)
    assert [(b.block_id, b.source_event_seq) for b in store.load_blocks(since_seq=1)] == [
        ("b", 2),
        ("c", 5),
    ]
    assert store.max_source_event_seq() == 5
    assert SqliteStore(tmp_path / "empty.db").max_source_event_seq() == 0


def test_load_blocks_default_includes_seq_zero(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "store.db")
    store.append(mk("z", 0, "zero"))
    assert [b.block_id for b in store.load_blocks()] == ["z"]
    assert store.load_blocks(since_seq=0) == []


def test_ingest_jsonl_skips_blank_lines_and_is_idempotent(tmp_path: Path) -> None:
    blocks = [mk("a", 1, "a"), mk("b", 2, "b")]
    path = tmp_path / "fabric.jsonl"
    path.write_text(
        "\n".join(["", blocks[0].model_dump_json(), "  ", blocks[1].model_dump_json(), ""]),
        encoding="utf-8",
    )
    store = SqliteStore(tmp_path / "store.db")
    assert store.ingest_jsonl(path) == (2, 0)
    assert store.ingest_jsonl(path) == (0, 2)
    assert [b.block_id for b in store.load_blocks()] == ["a", "b"]


def test_cli_ingest_and_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "fabric.jsonl"
    path.write_text(mk("a", 1, "a").model_dump_json() + "\n", encoding="utf-8")
    db = tmp_path / "store.db"
    monkeypatch.setattr(sys, "argv", ["python -m kernel.store", "ingest", str(path), str(db)])
    main()
    assert "1 inserted, 0 deduplicated" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["python -m kernel.store", "info", str(db)])
    main()
    out = capsys.readouterr().out
    assert "blocks=1" in out
    assert "max_source_event_seq=1" in out


def test_default_thread_affinity_and_check_same_thread_opt_in(tmp_path: Path) -> None:
    default_store = SqliteStore(tmp_path / "default.db")
    default_errors: list[Exception] = []

    def use_default_store() -> None:
        try:
            default_store.count()
        except Exception as exc:
            default_errors.append(exc)

    thread = threading.Thread(target=use_default_store)
    thread.start()
    thread.join()
    assert len(default_errors) == 1
    assert isinstance(default_errors[0], sqlite3.ProgrammingError)

    shared_store = SqliteStore(tmp_path / "shared.db", check_same_thread=False)
    errors: list[Exception] = []

    def append_from_thread() -> None:
        try:
            shared_store.append(mk("t1", 1, "from thread"))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    thread = threading.Thread(target=append_from_thread)
    thread.start()
    thread.join()
    assert errors == []
    assert shared_store.count() == 1
