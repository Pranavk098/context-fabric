# The Filing Cabinet — Durable Block Store (v0)

## What and why

Until now the fabric was amnesia: blocks lived in a JSONL file loaded at startup, and
"storing" a decision meant hand-editing that file. `docs/research/PATH-VIABILITY.md`
(§6 step 5) named the consequence — a plug with amnesia churns by day 2. Nothing
upstream works without memory that survives a restart.

`kernel/store/sqlite_store.py` is that memory, v0: every `ContextBlock` is appended to
a SQLite file in canonical-JSON form, survives restarts, and can be served by the
sidecar directly (`--fabric fabric.db`). Relations between blocks (`depends_on`,
`supersedes`, `contradicts`, ...) are stored and loaded by the same file, so closure can
run end-to-end from a DB-backed fabric.

## Why SQLite, not Postgres (yet)

- SQLite is stdlib — zero new dependencies, zero services, one file the user can copy.
- The Postgres schema (`kernel/migrations/001_initial_fabric_schema.sql`) remains the
  destination; a migration is a large solo-dev surface that beats nothing done at all.
  Ship the small contract now; the schema below is its strict subset.
- The contract (append-only, `block_id` uniqueness, content-hash conflict, canonical
  payloads, `source_event_seq` watermarking) is identical in both, so application code
  written against `SqliteStore` ports unchanged.

## Schema

```sql
CREATE TABLE blocks (
    store_seq        INTEGER PRIMARY KEY AUTOINCREMENT,  -- local append order
    block_id         TEXT NOT NULL,                      -- CPP block identity
    content_hash     TEXT NOT NULL,
    source_event_seq INTEGER NOT NULL,                   -- canonical_events.event_seq
    payload          TEXT NOT NULL,                      -- RFC 8785 canonical JSON
    UNIQUE (block_id)
);
CREATE INDEX idx_blocks_source_event_seq ON blocks (source_event_seq);

CREATE TABLE relations (
    rel_seq          INTEGER PRIMARY KEY AUTOINCREMENT,  -- local append order
    parent_id        TEXT NOT NULL,                      -- Relation[0] (source block)
    child_id         TEXT NOT NULL,                      -- Relation[1] (target block)
    relation_type    TEXT NOT NULL,                      -- Relation[2] ("depends_on", ...)
    payload          TEXT NOT NULL,                      -- RFC 8785 canonical JSON
    UNIQUE (parent_id, child_id, relation_type)
);
CREATE INDEX idx_relations_parent_id ON relations (parent_id);
CREATE INDEX idx_relations_child_id ON relations (child_id);
```

`payload` is the full block as canonical JSON (`protocol.canonical.canonicalize`), so a
future migration can decode rows into the typed `context_blocks` columns without loss.
A relation is the router's `Relation` tuple `(source_block_id, target_block_id, type)`
(`kernel/router/dependency_closer.py`); its canonical JSON payload is
`{"parent_id": ..., "child_id": ..., "relation_type": ...}` (keys sorted by JCS).
The store is opened with `PRAGMA journal_mode=WAL` for crash-safe writes.

## Semantics

- **Append-only.** `append(block)` returns `True` (inserted) or `False` (already present,
  idempotent). Re-appending the same `block_id` with a *different* `content_hash` raises
  `StoreConflict` — never a silent overwrite. This mirrors the `FoldConflict` contract
  in `kernel/epochs.py` (`EPOCH_FOLD`); the two types are independent.
- **Ordered reads.** `load_blocks(since_seq=-1)` (the default) returns all blocks; with an
  explicit `since_seq` it returns blocks with `source_event_seq > since_seq`, ordered by
  `store_seq`; `max_source_event_seq()` gives the high-water mark for incremental loads.
  Roundtrip is lossless (full model equality) for JSON-native payloads.
- **Counting.** `count()` and `max_source_event_seq()` (0 when empty).
- **Relations, same contract.** `append_relation(relation)` returns `True` (inserted) or
  `False` (identical triple and payload already present); the same
  `(parent_id, child_id, relation_type)` triple with a *different* payload raises
  `StoreConflict`. Because `append_relation` always writes the deterministic canonical
  payload, re-appending a triple can never conflict with itself; the conflict path exists
  for rows inserted out-of-band (another tool, a hand-edited DB) with a non-canonical
  payload, and `ingest_relations_jsonl` rejects non-string or empty ids before writing.
  `append_relations_many(relations)` returns `(inserted, deduplicated)`;
  `load_relations()` returns all relations ordered by `rel_seq` (append order);
  `relation_count()` counts rows.
- **Closure integration.** `load_relations()` feeds `compile_packet(..., relations=...)`
  unchanged, so the dependency closer (`depends_on` parent pull-in, `contradicts` pairing +
  `ConflictAlert`, `negative_knowledge` intersection) runs from stored data:

  ```python
  store = SqliteStore("fabric.db")
  result = compile_packet(
      scope=...,
      recipient=...,
      objective=...,
      stage=...,
      blocks=store.load_blocks(),
      relations=store.load_relations(),
  )
  result.trace["conflict_alerts"]  # alerts for stored contradictions
  ```

## Usage

```console
$ python -m kernel.store ingest fabric.jsonl fabric.db
fabric.db: 12 inserted, 0 deduplicated
$ python -m kernel.store ingest-relations relations.jsonl fabric.db
fabric.db: 8 inserted, 0 deduplicated
$ python -m kernel.store info fabric.db
fabric.db: blocks=12, max_source_event_seq=12, relations=8
$ python -m sidecar --upstream http://127.0.0.1:8000 --fabric fabric.db
```

`ingest-relations` reads one `Relation` JSON object per line (`parent_id`, `child_id`,
`relation_type`); blank lines are skipped. The sidecar dispatches on the filename:
`.db` / `.sqlite` → `SqliteStore.load_blocks()`; anything else → the JSONL reader
(unchanged).

## v0 limits (named, not hidden)

- **Single writer.** One process per store; concurrent writers are not coordinated.
  SQLite WAL handles crash recovery, not multi-process write arbitration. Within one
  process, `SqliteStore(path, check_same_thread=False)` opts into sharing the connection
  across threads (the sidecar does; it serializes writes with a lock). The default
  `check_same_thread=True` preserves sqlite3's thread affinity.
- **No cross-store joins.** Relations reference `block_id`s by value; the store does not
  verify the referenced blocks exist, and a store cannot join to another store's blocks.
  Referential integrity is the router's (closure ignores relations to unknown ids).
- **No typed columns or indexes** for scope/tier/kind/roles — filtering happens in
  Python after decode. Add columns (or move to Postgres) when block counts make this hurt.
- **Reads decode the payload every time.** Fine at v0 scale; the Postgres path is the
  answer when it is not.
