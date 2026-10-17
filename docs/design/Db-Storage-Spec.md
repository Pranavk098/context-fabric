# 03 — PostgreSQL Canonical Schema & Storage Specification

## 1. Storage Design Rules
1. **Append-Only Canonical Truth:** The `canonical_events` table is strictly append-only (`INSERT` only; `UPDATE` and `DELETE` are revoked for the application role)[cite: 21, 22, 23, 33].
2. **Watermarked Reads:** Every packet compilation pins a `watermark_event_seq` (`BIGINT`) from `canonical_events` so that concurrent writes never cause non-deterministic packet assembly[cite: 33].
3. **Bi-Temporal Validity:** Every `context_blocks` row tracks both `observed_at` (transaction time when the Fabric recorded it) and `valid_from` / `valid_to` (world validity window)[cite: 7, 24, 26, 40].

---

## 2. Complete PostgreSQL DDL (`001_initial_fabric_schema.sql`)

```sql
-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- 1. APPEND-ONLY CANONICAL EVENT LOG[cite: 23, 33]
CREATE TABLE canonical_events (
    event_seq           BIGSERIAL PRIMARY KEY,
    event_id            TEXT NOT NULL UNIQUE,          -- e.g., 'evt_01J9...'
    idempotency_key     TEXT NOT NULL UNIQUE,          -- Prevents duplicate ingestion[cite: 23, 32, 36]
    tenant_id           TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    workflow_id         TEXT NOT NULL,
    task_id             TEXT,
    agent_id            TEXT,
    event_type          TEXT NOT NULL,                 -- workflow.started, tool.result, decision.accepted, etc.[cite: 31]
    payload             JSONB NOT NULL,
    content_hash        TEXT NOT NULL,                 -- sha256 of canonical JSON payload[cite: 25, 32]
    observed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_events_scope ON canonical_events (tenant_id, project_id, workflow_id, event_seq);

-- 2. PROJECT CARTRIDGES (.cfx ZERO-TOKEN COLD BOOT)
CREATE TABLE project_cartridges (
    cartridge_id        TEXT PRIMARY KEY,              -- e.g., 'cfx_payments_01'
    tenant_id           TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    repo_commit_hash    TEXT NOT NULL,
    hologram_markdown   TEXT NOT NULL,                 -- Rendered Tier 0 structural map
    ast_symbol_graph    JSONB NOT NULL,                -- Export/import adjacency list for PageRank
    token_count         INTEGER NOT NULL,
    kv_prefix_hash      TEXT NOT NULL,                 -- Deterministic hash for KV cache pinning
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, project_id, repo_commit_hash)
);

-- 3. DERIVED CONTEXT BLOCKS[cite: 23, 26]
CREATE TABLE context_blocks (
    block_id            TEXT PRIMARY KEY,              -- e.g., 'blk_01J9...'[cite: 26]
    source_event_seq    BIGINT NOT NULL REFERENCES canonical_events(event_seq),
    tenant_id           TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    workflow_id         TEXT,
    task_id             TEXT,
    block_kind          TEXT NOT NULL CHECK (block_kind IN (
        'instruction', 'decision', 'fact', 'artifact', 'observation',
        'summary', 'pointer', 'negative_knowledge', 'shadow_probe', 'cartridge_hologram'
    )),
    memory_tier         SMALLINT NOT NULL CHECK (memory_tier IN (0, 1, 2, 3)),
    media_type          TEXT NOT NULL DEFAULT 'text/markdown',
    content             TEXT,                          -- Null if pure pointer[cite: 26]
    ref_uri             TEXT,                          -- e.g., 'ctx://object/...'[cite: 26]
    content_hash        TEXT NOT NULL,                 -- sha256[cite: 25, 32]
    token_estimate      INTEGER NOT NULL,
    tokenizer_name      TEXT NOT NULL DEFAULT 'o200k_base',
    
    -- Provenance & Bi-Temporal Metadata[cite: 24, 26]
    creator_type        TEXT NOT NULL CHECK (creator_type IN ('human', 'agent', 'tool', 'deterministic_probe')),
    creator_id          TEXT NOT NULL,
    confidence          REAL NOT NULL DEFAULT 1.0,
    fidelity            TEXT NOT NULL CHECK (fidelity IN ('raw', 'extract', 'summary', 'pointer')),
    verified            BOOLEAN NOT NULL DEFAULT FALSE,
    observed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_from          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_to            TIMESTAMPTZ,                   -- NULL means currently valid[cite: 26]
    lifecycle_status    TEXT NOT NULL DEFAULT 'routable' CHECK (lifecycle_status IN (
        'draft', 'validated', 'routable', 'superseded', 'expired', 'redacted'
    )),                                                --[cite: 26]

    -- Governance & Policy Metadata[cite: 26, 32]
    classification      TEXT NOT NULL DEFAULT 'internal',
    trust_domain        TEXT NOT NULL,
    allowed_roles       TEXT[] NOT NULL DEFAULT '{}',  -- Empty = all roles in trust_domain
    denied_roles        TEXT[] NOT NULL DEFAULT '{}',  -- e.g., {'auditor'} for persuasive observations[cite: 30]
    allowed_purposes    TEXT[] NOT NULL DEFAULT '{}',

    -- Negative Knowledge / Counterfactual Payload
    counterfactual_meta JSONB,                         -- rejected_action, failure_evidence_ref, blast_radius

    -- Hybrid Search Indexes (Lexical + Semantic + Symbols)[cite: 22, 23, 27]
    symbols_exported    TEXT[] NOT NULL DEFAULT '{}',
    symbols_imported    TEXT[] NOT NULL DEFAULT '{}',
    search_tsv          TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', COALESCE(content, ''))) STORED,
    embedding           VECTOR(1536)                   -- Nullable if embedding service degraded[cite: 33]
);

CREATE INDEX idx_blocks_scope_valid ON context_blocks (tenant_id, project_id, lifecycle_status, valid_to);
CREATE INDEX idx_blocks_tsv ON context_blocks USING GIN (search_tsv);
CREATE INDEX idx_blocks_symbols_exp ON context_blocks USING GIN (symbols_exported);
CREATE INDEX idx_blocks_symbols_imp ON context_blocks USING GIN (symbols_imported);
CREATE INDEX idx_blocks_embedding ON context_blocks USING hnsw (embedding vector_cosine_ops);

-- 4. BLOCK RELATIONS (DEPENDENCY, SUPERSESSION, CONTRADICTION GRAPH)[cite: 26, 33]
CREATE TABLE block_relations (
    relation_id         BIGSERIAL PRIMARY KEY,
    source_block_id     TEXT NOT NULL REFERENCES context_blocks(block_id),
    target_block_id     TEXT NOT NULL REFERENCES context_blocks(block_id),
    relation_type       TEXT NOT NULL CHECK (relation_type IN (
        'depends_on', 'supersedes', 'contradicts', 'supports', 'derived_from'
    )),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_block_id, target_block_id, relation_type)
);

CREATE INDEX idx_relations_source ON block_relations (source_block_id, relation_type);
CREATE INDEX idx_relations_target ON block_relations (target_block_id, relation_type);

-- 5. IMMUTABLE COMPILED PACKETS & ROUTE TRACES[cite: 24, 25, 36]
CREATE TABLE compiled_packets (
    packet_id           TEXT PRIMARY KEY,              -- 'cpk_...'[cite: 25]
    parent_packet_id    TEXT REFERENCES compiled_packets(packet_id),
    route_id            TEXT NOT NULL UNIQUE,          -- 'rte_...'[cite: 25]
    watermark_event_seq BIGINT NOT NULL REFERENCES canonical_events(event_seq),
    tenant_id           TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    workflow_id         TEXT NOT NULL,
    task_id             TEXT NOT NULL,
    recipient_agent_id  TEXT NOT NULL,
    recipient_role      TEXT NOT NULL,
    spawn_depth         INTEGER NOT NULL DEFAULT 0,
    epoch_number        INTEGER NOT NULL DEFAULT 1,
    packet_json         JSONB NOT NULL,                -- Full immutable CPP v1.0 logical payload[cite: 24, 25]
    route_trace_json    JSONB NOT NULL,                -- Inclusion/exclusion reasons, scores, policy verdicts[cite: 9, 11, 21]
    kv_prefix_hash      TEXT NOT NULL,
    content_hash        TEXT NOT NULL,                 -- sha256[cite: 25]
    signature           TEXT,                          -- ed25519 signature[cite: 25, 32]
    total_tokens        INTEGER NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at          TIMESTAMPTZ
);

-- 6. DELIVERY RECEIPTS & OUTCOME FEEDBACK[cite: 22, 23, 36]
CREATE TABLE packet_receipts (
    receipt_id          TEXT PRIMARY KEY,
    packet_id           TEXT NOT NULL REFERENCES compiled_packets(packet_id),
    agent_id            TEXT NOT NULL,
    adapter_name        TEXT NOT NULL,
    kv_cache_hit_ratio  REAL,                          -- Observed cache hit rate from provider/vLLM
    compile_latency_ms  REAL NOT NULL,
    cited_block_ids     TEXT[] NOT NULL DEFAULT '{}',  -- Blocks actually used by the agent[cite: 22, 24]
    missing_context_gap JSONB,                         -- Recorded if agent requested PAGE_FAULT_EXPAND[cite: 22, 30]
    task_succeeded      BOOLEAN,
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);