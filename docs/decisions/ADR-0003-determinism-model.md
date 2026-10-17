# ADR-0003 — Determinism Model

- **Status:** Accepted
- **Date:** 2026-10-16

## Context

Invariant 9 ("deterministic core") and Invariant 10 ("cryptographic replayability") require
that the same inputs always produce the same packet bytes and the same content hash. Four
sources of nondeterminism were left unspecified: embeddings, MMR tie-breaking, PageRank
convergence, and signing latency.

## Decision

1. **No learned components in v1.0 core.** Retrieval uses BM25 (`tsvector`) + symbol-graph
   + explicit refs unconditionally; `pgvector` recall is an *optional* augment behind a flag.
2. **Embedding version pinning.** If embeddings are enabled, the model id, revision, and
   distance metric are recorded on each block; a packet compiled with a different embedding
   revision is flagged `embedding_revision_mismatch` and is **not** replay-comparable.
   Degraded mode (embeddings down) is BM25-only and is deterministic.
3. **MMR tie-breaking.** All candidate scores are rounded to a fixed precision (6 dp) before
   comparison; ties are broken by ascending `block_id` (lexicographic). Selection is a total
   order, hence reproducible.
4. **PageRank.** `alpha = 0.85`, fixed max iterations (default 100), tolerance `1e-9`;
   the graph is built from a deterministically ordered edge list (sorted by file path,
   then symbol name). Cartridge `kv_prefix_hash` is computed over the canonicalized
   rendered hologram, not over the raw graph.
5. **Signing is off the critical path.** Ed25519 signing is fast (<1 ms) but the `hashes`
   block is emptied before hashing: both `signature` and `content_hash` itself are excluded,
   so the hash is idempotent and signature-independent (`packet_content_hash`). Keys are
   identified by `key_id` recorded in the route trace.
6. **Tokenizer pinned.** `tokenizer_name` is a field on every block and on the packet
   (default `o200k_base`); token counts are computed with the pinned tokenizer only.

## Consequences

- Replayability becomes testable: recompile from the same watermark and assert identical
  `content_hash`.
- Nondeterminism is confined to one explicitly-flagged, optional path (embeddings).
- The adapter may re-render prose (e.g. provider-specific wrapper text), but the **logical
  packet hash must be identical across adapters** — this is the Semantic Parity Gate.
