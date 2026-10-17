-- Context Fabric — canonical PostgreSQL schema (CPP v1.0)
-- Append-only canonical truth; derived blocks; bi-temporal; immutable packets & receipts.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- 1. APPEND-ONLY CANONICAL EVENT LOG
CREATE TABLE canonical_events (
    event_seq           BIGSERIAL PRIMARY KEY,
    event_id            TEXT NOT NULL UNIQUE,
    idempotency_key     TEXT NOT NULL UNIQUE,
    tenant_id           TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    workflow_id         TEXT NOT NULL,
    task_id             TEXT,
    agent_id            TEXT,
    event_type          TEXT NOT NULL,
    payload             JSONB NOT NULL,
    content_hash        TEXT NOT NULL,
    observed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_events_scope ON canonical_events (tenant_id, project_id, workflow_id, event_seq);

-- 2. PROJECT CARTRIDGES (.cfx ZERO-TOKEN COLD BOOT)
CREATE TABLE project_cartridges (
    cartridge_id        TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    repo_commit_hash    TEXT NOT NULL,
    hologram_markdown   TEXT NOT NULL,
    ast_symbol_graph    JSONB NOT NULL,
    token_count         INTEGER NOT NULL,
    kv_prefix_hash      TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, project_id, repo_commit_hash)
);

-- 3. DERIVED CONTEXT BLOCKS
CREATE TABLE context_blocks (
    block_id            TEXT PRIMARY KEY,
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
    content             TEXT,
    ref_uri             TEXT,
    content_hash        TEXT NOT NULL,
    token_estimate      INTEGER NOT NULL,
    tokenizer_name      TEXT NOT NULL DEFAULT 'o200k_base',
    creator_type        TEXT NOT NULL CHECK (creator_type IN ('human', 'agent', 'tool', 'deterministic_probe')),
    creator_id          TEXT NOT NULL,
    confidence          REAL NOT NULL DEFAULT 1.0,
    fidelity            TEXT NOT NULL CHECK (fidelity IN ('raw', 'extract', 'summary', 'pointer')),
    verified            BOOLEAN NOT NULL DEFAULT FALSE,
    observed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_from          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_to            TIMESTAMPTZ,
    lifecycle_status    TEXT NOT NULL DEFAULT 'routable' CHECK (lifecycle_status IN (
        'draft', 'validated', 'routable', 'superseded', 'expired', 'redacted'
    )),
    classification      TEXT NOT NULL DEFAULT 'internal',
    trust_domain        TEXT NOT NULL,
    allowed_roles       TEXT[] NOT NULL DEFAULT '{}',
    denied_roles        TEXT[] NOT NULL DEFAULT '{}',
    allowed_purposes    TEXT[] NOT NULL DEFAULT '{}',
    counterfactual_meta JSONB,
    symbols_exported    TEXT[] NOT NULL DEFAULT '{}',
    symbols_imported    TEXT[] NOT NULL DEFAULT '{}',
    search_tsv          TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', COALESCE(content, ''))) STORED,
    embedding           VECTOR(1536)
);
CREATE INDEX idx_blocks_scope_valid ON context_blocks (tenant_id, project_id, lifecycle_status, valid_to);
CREATE INDEX idx_blocks_tsv ON context_blocks USING GIN (search_tsv);
CREATE INDEX idx_blocks_symbols_exp ON context_blocks USING GIN (symbols_exported);
CREATE INDEX idx_blocks_symbols_imp ON context_blocks USING GIN (symbols_imported);
CREATE INDEX idx_blocks_embedding ON context_blocks USING hnsw (embedding vector_cosine_ops);

-- 4. BLOCK RELATIONS (DEPENDENCY, SUPERSESSION, CONTRADICTION GRAPH)
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

-- 5. IMMUTABLE COMPILED PACKETS & ROUTE TRACES
CREATE TABLE compiled_packets (
    packet_id           TEXT PRIMARY KEY,
    parent_packet_id    TEXT REFERENCES compiled_packets(packet_id),
    route_id            TEXT NOT NULL UNIQUE,
    watermark_event_seq BIGINT NOT NULL REFERENCES canonical_events(event_seq),
    tenant_id           TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    workflow_id         TEXT NOT NULL,
    task_id             TEXT NOT NULL,
    recipient_agent_id  TEXT NOT NULL,
    recipient_role      TEXT NOT NULL,
    spawn_depth         INTEGER NOT NULL DEFAULT 0,
    epoch_number        INTEGER NOT NULL DEFAULT 1,
    packet_json         JSONB NOT NULL,
    route_trace_json    JSONB NOT NULL,
    kv_prefix_hash      TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    signature           TEXT,
    total_tokens        INTEGER NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at          TIMESTAMPTZ
);

-- 6. DELIVERY RECEIPTS & OUTCOME FEEDBACK
CREATE TABLE packet_receipts (
    receipt_id          TEXT PRIMARY KEY,
    packet_id           TEXT NOT NULL REFERENCES compiled_packets(packet_id),
    agent_id            TEXT NOT NULL,
    adapter_name        TEXT NOT NULL,
    kv_cache_hit_ratio  REAL,
    compile_latency_ms  REAL NOT NULL,
    cited_block_ids     TEXT[] NOT NULL DEFAULT '{}',
    missing_context_gap JSONB,
    task_succeeded      BOOLEAN,
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
