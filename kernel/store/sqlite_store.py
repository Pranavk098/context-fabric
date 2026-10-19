"""SQLite-backed durable store for ContextBlock records — the filing cabinet, v0.

Stdlib ``sqlite3`` only: append-only rows, canonical-JSON payloads, WAL for
crash-safe writes. The Postgres schema (``kernel/migrations/001_initial_fabric_schema.sql``)
is the future path; this store is deliberately the smaller contract.

The same file also carries ``compile_log`` — one ``CompileManifest`` per successful sidecar
compile, the replayable receipt of what was handed to ``compile_packet`` (``cfx replay``).

Parallel contract: ``kernel/epochs.py`` (EPOCH_FOLD) defines ``FoldConflict`` for the
same reason this module defines ``StoreConflict`` — a reused id with divergent content
is a hard error, never a silent overwrite. The two types are independent; neither
module imports the other.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from kernel.router.dependency_closer import Relation
from protocol.canonical import canonicalize
from protocol.models import ContextBlock, Recipient, Scope

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
CREATE TABLE IF NOT EXISTS compile_log (
    log_seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_id           TEXT UNIQUE NOT NULL,
    content_hash        TEXT NOT NULL,
    scope_json          TEXT NOT NULL,
    recipient_json      TEXT NOT NULL,
    objective           TEXT NOT NULL,
    stage               TEXT NOT NULL,
    as_of               TEXT NOT NULL,
    watermark_event_seq INTEGER NOT NULL,
    budget_base         INTEGER NOT NULL,
    block_ids           TEXT NOT NULL,
    block_hashes        TEXT NOT NULL,
    model               TEXT,
    engine              TEXT,
    purpose             TEXT,
    created_at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fold_log (
    fold_seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    message_index   INTEGER NOT NULL,
    stub_id         TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    tool_name       TEXT NOT NULL,
    content_bytes   INTEGER NOT NULL,
    seen_hash       TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fold_log_session ON fold_log (session_id);
"""

_MANIFEST_COLUMNS = (
    "packet_id, content_hash, scope_json, recipient_json, objective, stage, as_of,"
    " watermark_event_seq, budget_base, block_ids, block_hashes, model, engine, purpose, created_at"
)


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


@dataclass(frozen=True)
class CompileManifest:
    """The exact inputs of one successful ``compile_packet`` run — a replay receipt.

    ``block_ids``/``block_hashes`` are parallel, sorted tuples naming every block handed
    to the compiler, so ``cfx replay`` can rebuild that input from the store and recompile
    byte-for-byte. ``as_of`` is the value actually passed to the compiler, not a new
    wall-clock read. ``model``/``engine`` are operator-reported stack metadata ("" when
    unknown); the manifest never invents a model name.
    """

    packet_id: str
    content_hash: str
    scope: Scope
    recipient: Recipient
    objective: str
    stage: str
    as_of: datetime
    watermark_event_seq: int
    budget_base: int
    block_ids: tuple[str, ...]
    block_hashes: tuple[str, ...]
    model: str
    engine: str
    purpose: str | None
    created_at: datetime


def _manifest_from_row(row: tuple[Any, ...]) -> CompileManifest:
    (
        packet_id,
        content_hash,
        scope_json,
        recipient_json,
        objective,
        stage,
        as_of,
        watermark_event_seq,
        budget_base,
        block_ids,
        block_hashes,
        model,
        engine,
        purpose,
        created_at,
    ) = row
    return CompileManifest(
        packet_id=packet_id,
        content_hash=content_hash,
        scope=Scope.model_validate_json(scope_json),
        recipient=Recipient.model_validate_json(recipient_json),
        objective=objective,
        stage=stage,
        as_of=datetime.fromisoformat(as_of),
        watermark_event_seq=int(watermark_event_seq),
        budget_base=int(budget_base),
        block_ids=tuple(json.loads(block_ids)),
        block_hashes=tuple(json.loads(block_hashes)),
        model=model or "",
        engine=engine or "",
        purpose=purpose if purpose else None,
        created_at=datetime.fromisoformat(created_at),
    )


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
        # Stores created before compile_log.purpose existed: add the column in place.
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(compile_log)")}
        if "purpose" not in columns:
            self._conn.execute("ALTER TABLE compile_log ADD COLUMN purpose TEXT")
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

    def log_compile(self, manifest: CompileManifest) -> None:
        """Record ``manifest``; first write for a ``packet_id`` wins.

        ``packet_id`` is UNIQUE: a later log for an already-recorded ``packet_id`` —
        identical or divergent — is silently ignored, so the first compile logged for a
        packet identity stays the receipt and re-logging is idempotent.
        """
        if len(manifest.block_ids) != len(manifest.block_hashes):
            raise ValueError("block_ids and block_hashes must have the same length")
        self._conn.execute(
            "INSERT OR IGNORE INTO compile_log"
            " (packet_id, content_hash, scope_json, recipient_json, objective, stage, as_of,"
            " watermark_event_seq, budget_base, block_ids, block_hashes, model, engine, purpose,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                manifest.packet_id,
                manifest.content_hash,
                canonicalize(manifest.scope.model_dump(mode="json")).decode("utf-8"),
                canonicalize(manifest.recipient.model_dump(mode="json")).decode("utf-8"),
                manifest.objective,
                manifest.stage,
                manifest.as_of.isoformat(),
                manifest.watermark_event_seq,
                manifest.budget_base,
                json.dumps(list(manifest.block_ids)),
                json.dumps(list(manifest.block_hashes)),
                manifest.model,
                manifest.engine,
                manifest.purpose,
                manifest.created_at.isoformat(),
            ),
        )
        self._conn.commit()

    def get_compile(self, packet_id: str) -> CompileManifest | None:
        """The logged manifest for ``packet_id``, or ``None``."""
        row = self._conn.execute(
            f"SELECT {_MANIFEST_COLUMNS} FROM compile_log WHERE packet_id = ?", (packet_id,)
        ).fetchone()
        return None if row is None else _manifest_from_row(row)

    def load_compile_log(self, limit: int | None = 100) -> list[CompileManifest]:
        """Manifests in append order (``log_seq``), capped at ``limit``; ``None`` = all."""
        sql = f"SELECT {_MANIFEST_COLUMNS} FROM compile_log ORDER BY log_seq"
        if limit is not None:
            sql += " LIMIT ?"
            rows = self._conn.execute(sql, (limit,)).fetchall()
        else:
            rows = self._conn.execute(sql).fetchall()
        return [_manifest_from_row(row) for row in rows]

    def log_folds(
        self,
        session_id: str,
        events: Iterable[dict[str, Any]],
        seen_hash: str,
        created_at: datetime,
    ) -> int:
        """Append one row per fold event to the append-only ``fold_log`` receipt.

        ``events`` are plain dicts (``message_index``, ``stub_id``, ``content_hash``,
        ``tool_name``, ``content_bytes``); ``seen_hash`` is the sha256 of the folded
        message list the model saw, denormalized onto every event row of that run.
        """
        rows = [
            (
                session_id,
                int(event["message_index"]),
                event["stub_id"],
                event["content_hash"],
                event["tool_name"],
                int(event["content_bytes"]),
                seen_hash,
                created_at.isoformat(),
            )
            for event in events
        ]
        if not rows:
            return 0
        self._conn.executemany(
            "INSERT INTO fold_log"
            " (session_id, message_index, stub_id, content_hash, tool_name, content_bytes,"
            " seen_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()
        return len(rows)

    def load_fold_log(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Fold-event receipt rows in append order (``fold_seq``), optionally per session."""
        if session_id is None:
            rows = self._conn.execute(
                "SELECT fold_seq, session_id, message_index, stub_id, content_hash, tool_name,"
                " content_bytes, seen_hash, created_at FROM fold_log ORDER BY fold_seq"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT fold_seq, session_id, message_index, stub_id, content_hash, tool_name,"
                " content_bytes, seen_hash, created_at FROM fold_log"
                " WHERE session_id = ? ORDER BY fold_seq",
                (session_id,),
            ).fetchall()
        return [
            {
                "fold_seq": int(row[0]),
                "session_id": row[1],
                "message_index": int(row[2]),
                "stub_id": row[3],
                "content_hash": row[4],
                "tool_name": row[5],
                "content_bytes": int(row[6]),
                "seen_hash": row[7],
                "created_at": row[8],
            }
            for row in rows
        ]
