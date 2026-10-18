"""SQLite-backed durable store for ContextBlock records — the filing cabinet, v0.

Stdlib ``sqlite3`` only: append-only rows, canonical-JSON payloads, WAL for
crash-safe writes. The Postgres schema (``kernel/migrations/001_initial_fabric_schema.sql``)
is the future path; this store is deliberately the smaller contract.

Parallel contract: ``kernel/epochs.py`` (EPOCH_FOLD) defines ``FoldConflict`` for the
same reason this module defines ``StoreConflict`` — a reused id with divergent content
is a hard error, never a silent overwrite. The two types are independent; neither
module imports the other.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from pydantic import ValidationError

from kernel.router.dependency_closer import Relation
from protocol.canonical import canonicalize
from protocol.models import ContextBlock

_SCHEMA = """
CREATE TABLE IF NOT EXISTS blocks (
    store_seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    block_id         TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    source_event_seq INTEGER NOT NULL,
    payload          TEXT NOT NULL,
    UNIQUE (block_id)
);
CREATE INDEX IF NOT EXISTS idx_blocks_source_event_seq ON blocks (source_event_seq);
CREATE TABLE IF NOT EXISTS relations (
    rel_seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id        TEXT NOT NULL,
    child_id         TEXT NOT NULL,
    relation_type    TEXT NOT NULL,
    payload          TEXT NOT NULL,
    UNIQUE (parent_id, child_id, relation_type)
);
CREATE INDEX IF NOT EXISTS idx_relations_parent_id ON relations (parent_id);
CREATE INDEX IF NOT EXISTS idx_relations_child_id ON relations (child_id);
"""


def _relation_payload(relation: Relation) -> str:
    parent_id, child_id, relation_type = relation
    return canonicalize(
        {"parent_id": parent_id, "child_id": child_id, "relation_type": relation_type}
    ).decode("utf-8")


def _relation_from_json(text: str) -> Relation:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("relation must be a JSON object")
    try:
        relation = (data["parent_id"], data["child_id"], data["relation_type"])
    except KeyError as exc:
        raise ValueError(f"missing relation field {exc}") from exc
    if not all(isinstance(part, str) and part for part in relation):
        raise ValueError("relation ids and type must be non-empty strings")
    return relation


class StoreConflict(Exception):
    """A ``block_id`` was appended twice with different content hashes."""


class SqliteStore:
    """Append-only store of ContextBlock payloads, one SQLite file per store.

    Single-writer assumption (v0): use one store per process. SQLite's WAL handles
    crash recovery; concurrent writers to the same file are not coordinated here.

    ``check_same_thread=False`` allows one connection to be shared by server threads
    (the sidecar does this); callers must then serialize write transactions. The
    default keeps sqlite3's thread affinity, identical to earlier behavior.
    """

    def __init__(self, path: str | Path, *, check_same_thread: bool = True) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=check_same_thread)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def append(self, block: ContextBlock) -> bool:
        """Append ``block``; ``True`` if inserted, ``False`` if already present.

        Raises ``StoreConflict`` when the same ``block_id`` is already stored with a
        different ``content_hash``.
        """
        payload = canonicalize(block.model_dump(mode="json")).decode("utf-8")
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO blocks (block_id, content_hash, source_event_seq, payload)"
            " VALUES (?, ?, ?, ?)",
            (block.block_id, block.content_hash, block.source_event_seq, payload),
        )
        self._conn.commit()
        if cursor.rowcount > 0:
            return True
        existing = self._conn.execute(
            "SELECT content_hash FROM blocks WHERE block_id = ?", (block.block_id,)
        ).fetchone()
        if existing is not None and existing[0] != block.content_hash:
            raise StoreConflict(
                f"block {block.block_id!r} already stored with content_hash {existing[0]!r}, "
                f"refusing {block.content_hash!r}"
            )
        return False

    def append_many(self, blocks: Iterable[ContextBlock]) -> tuple[int, int]:
        """Append many blocks; returns ``(inserted, deduplicated)``."""
        inserted = deduplicated = 0
        for block in blocks:
            if self.append(block):
                inserted += 1
            else:
                deduplicated += 1
        return inserted, deduplicated

    def load_blocks(self, since_seq: int = -1) -> list[ContextBlock]:
        """Blocks with ``source_event_seq > since_seq`` (default -1: all blocks)."""
        rows = self._conn.execute(
            "SELECT payload FROM blocks WHERE source_event_seq > ? ORDER BY store_seq",
            (since_seq,),
        ).fetchall()
        return [ContextBlock.model_validate_json(row[0]) for row in rows]

    def count(self) -> int:
        """Number of stored blocks."""
        row = self._conn.execute("SELECT COUNT(*) FROM blocks").fetchone()
        return int(row[0])

    def max_source_event_seq(self) -> int:
        """Highest stored ``source_event_seq`` (0 when empty)."""
        row = self._conn.execute("SELECT COALESCE(MAX(source_event_seq), 0) FROM blocks").fetchone()
        return int(row[0])

    def ingest_jsonl(self, path: str | Path) -> tuple[int, int]:
        """Ingest the sidecar JSONL format (one ContextBlock JSON per line)."""
        blocks: list[ContextBlock] = []
        for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                try:
                    blocks.append(ContextBlock.model_validate_json(line))
                except ValidationError as exc:
                    raise ValueError(f"{path}:{lineno}: {exc}") from exc
        return self.append_many(blocks)

    def append_relation(self, relation: Relation) -> bool:
        """Append ``relation``; ``True`` if inserted, ``False`` if already present.

        Raises ``StoreConflict`` when the same ``(parent_id, child_id, relation_type)``
        triple is already stored with a different payload.
        """
        payload = _relation_payload(relation)
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO relations (parent_id, child_id, relation_type, payload)"
            " VALUES (?, ?, ?, ?)",
            (*relation, payload),
        )
        self._conn.commit()
        if cursor.rowcount > 0:
            return True
        existing = self._conn.execute(
            "SELECT payload FROM relations"
            " WHERE parent_id = ? AND child_id = ? AND relation_type = ?",
            relation,
        ).fetchone()
        if existing is not None and existing[0] != payload:
            raise StoreConflict(
                f"relation {relation!r} already stored with payload {existing[0]!r},"
                f" refusing {payload!r}"
            )
        return False

    def append_relations_many(self, relations: Iterable[Relation]) -> tuple[int, int]:
        """Append many relations; returns ``(inserted, deduplicated)``."""
        inserted = deduplicated = 0
        for relation in relations:
            if self.append_relation(relation):
                inserted += 1
            else:
                deduplicated += 1
        return inserted, deduplicated

    def load_relations(self) -> list[Relation]:
        """All relations, ordered by ``rel_seq`` (local append order)."""
        rows = self._conn.execute("SELECT payload FROM relations ORDER BY rel_seq").fetchall()
        return [_relation_from_json(row[0]) for row in rows]

    def relation_count(self) -> int:
        """Number of stored relations."""
        row = self._conn.execute("SELECT COUNT(*) FROM relations").fetchone()
        return int(row[0])

    def ingest_relations_jsonl(self, path: str | Path) -> tuple[int, int]:
        """Ingest a JSONL file with one Relation JSON object per line."""
        relations: list[Relation] = []
        for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                try:
                    relations.append(_relation_from_json(line))
                except (ValueError, KeyError, TypeError) as exc:
                    raise ValueError(f"{path}:{lineno}: {exc}") from exc
        return self.append_relations_many(relations)
