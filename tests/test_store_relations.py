"""Relation persistence tests — roundtrip, dedupe, conflict, JSONL/CLI, DB-backed closure."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kernel.router.compiler import compile_packet
from kernel.router.dependency_closer import Relation
from kernel.store.__main__ import main
from kernel.store.sqlite_store import SqliteStore, StoreConflict
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

SCOPE = Scope(tenant_id="t1", project_id="p1", workflow_id="w1", task_id="task1")
AS_OF = datetime(2026, 1, 1, tzinfo=UTC)

RELATIONS: list[Relation] = [
    ("a", "b", "depends_on"),
    ("b", "c", "supports"),
    ("c", "d", "derived_from"),
    ("d", "e", "supersedes"),
    ("e", "f", "contradicts"),
    ("blk_\u00e9", "f", "supports"),
]


def mk(bid: str, kind: BlockKind, seq: int, content: str) -> ContextBlock:
    return ContextBlock(
        block_id=bid,
        block_kind=kind,
        memory_tier=MemoryTier.ledger,
        content=content,
        content_hash="sha256:" + bid,
        token_estimate=8,
        source_event_seq=seq,
        creator=Creator(type=CreatorType.agent, id="a"),
        fidelity=Fidelity.extract,
        trust_domain="t1",
    )


def rjson(relation: Relation) -> str:
    parent_id, child_id, relation_type = relation
    return json.dumps(
        {"parent_id": parent_id, "child_id": child_id, "relation_type": relation_type}
    )


def test_relation_roundtrip_is_lossless(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "store.db")
    for relation in RELATIONS:
        assert store.append_relation(relation) is True
    assert store.load_relations() == RELATIONS
    assert store.relation_count() == len(RELATIONS)


def test_reappend_relation_is_deduplicated(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "store.db")
    relation = ("a", "b", "contradicts")
    assert store.append_relation(relation) is True
    assert store.append_relation(relation) is False
    assert store.relation_count() == 1


def test_same_triple_with_different_payload_raises(tmp_path: Path) -> None:
    db = tmp_path / "store.db"
    store = SqliteStore(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO relations (parent_id, child_id, relation_type, payload) VALUES (?, ?, ?, ?)",
        (
            "a",
            "b",
            "depends_on",
            '{ "parent_id": "a", "child_id": "b", "relation_type": "depends_on" }',
        ),
    )
    conn.commit()
    conn.close()
    with pytest.raises(StoreConflict):
        store.append_relation(("a", "b", "depends_on"))
    assert store.relation_count() == 1
    assert store.load_relations() == [("a", "b", "depends_on")]


def test_append_relations_many_counts(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "store.db")
    assert store.append_relations_many(RELATIONS) == (len(RELATIONS), 0)
    assert store.append_relations_many(RELATIONS) == (0, len(RELATIONS))
    assert store.relation_count() == len(RELATIONS)


def test_ingest_relations_jsonl_skips_blank_lines_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "relations.jsonl"
    path.write_text(
        "\n".join(["", rjson(RELATIONS[0]), "  ", rjson(RELATIONS[1]), ""]),
        encoding="utf-8",
    )
    store = SqliteStore(tmp_path / "store.db")
    assert store.ingest_relations_jsonl(path) == (2, 0)
    assert store.ingest_relations_jsonl(path) == (0, 2)
    assert store.load_relations() == RELATIONS[:2]


def test_ingest_relations_jsonl_reports_bad_line(tmp_path: Path) -> None:
    path = tmp_path / "relations.jsonl"
    path.write_text('{"parent_id": "a"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=":1: "):
        SqliteStore(tmp_path / "store.db").ingest_relations_jsonl(path)


@pytest.mark.parametrize(
    "line",
    [
        '{"parent_id": 1, "child_id": "b", "relation_type": "supports"}',
        '{"parent_id": "", "child_id": "b", "relation_type": "supports"}',
        '["a", "b", "supports"]',
    ],
)
def test_ingest_relations_jsonl_rejects_non_string_ids(tmp_path: Path, line: str) -> None:
    path = tmp_path / "relations.jsonl"
    path.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=":1: "):
        SqliteStore(tmp_path / "store.db").ingest_relations_jsonl(path)
    assert SqliteStore(tmp_path / "store.db").relation_count() == 0


def test_cli_ingest_relations_and_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "relations.jsonl"
    path.write_text(rjson(("a", "b", "depends_on")) + "\n", encoding="utf-8")
    db = tmp_path / "store.db"
    monkeypatch.setattr(
        sys, "argv", ["python -m kernel.store", "ingest-relations", str(path), str(db)]
    )
    main()
    assert "1 inserted, 0 deduplicated" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["python -m kernel.store", "info", str(db)])
    main()
    assert "relations=1" in capsys.readouterr().out


def test_db_backed_closure_end_to_end(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "store.db")
    store.append(mk("dec", BlockKind.decision, 1, "adopt sqlite storage"))
    store.append(mk("dep", BlockKind.fact, 2, "quartz veblen hysteresis"))
    store.append(mk("con", BlockKind.fact, 3, "zugzwang kerfuffle"))
    assert store.append_relation(("dec", "dep", "depends_on")) is True
    assert store.append_relation(("dec", "con", "contradicts")) is True

    result = compile_packet(
        scope=SCOPE,
        recipient=Recipient(agent_id="a1", role="implementer", stage="implement"),
        objective="decide the storage backend",
        stage="implement",
        blocks=store.load_blocks(),
        relations=store.load_relations(),
        as_of=AS_OF,
    )
    included = result.trace["included"]
    assert "dep" in included
    assert "con" in included
    assert {"a_block_id": "dec", "b_block_id": "con", "reason": "contradicts"} in result.trace[
        "conflict_alerts"
    ]

    without_relations = compile_packet(
        scope=SCOPE,
        recipient=Recipient(agent_id="a1", role="implementer", stage="implement"),
        objective="decide the storage backend",
        stage="implement",
        blocks=store.load_blocks(),
        as_of=AS_OF,
    )
    assert "dep" not in without_relations.trace["included"]
    assert without_relations.trace["conflict_alerts"] == []
