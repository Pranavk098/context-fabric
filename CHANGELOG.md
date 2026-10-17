# Changelog

All notable changes to Context Fabric are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- **The plug** — OpenAI-compatible sidecar proxy (`sidecar/`): compile/passthrough modes, SSE
  streaming, stats; ~10-minute setup (`docs/engineering/SIDECAR.md`).
- **The seal** — signed epoch chain + receipts (`protocol/epoch.py`, `verify` CLI), cross-process
  determinism spike, tamper tests (`docs/engineering/SEAL.md`).
- **The shared copy** — memory-state copy-on-write (`kernel/epochs.py`): O(1) fork, private
  edits, explicit fold with deterministic conflict rejection (`docs/engineering/SHARED-COPY.md`).
- **The filing cabinet (v0)** — durable SQLite store (`kernel/store/`) + sidecar `.db` loading
  (`docs/engineering/STORE.md`).
- **Stage C kernel** (`kernel/`) — deterministic `.cfx` cartridge compiler (stdlib `ast`
  extractor + PageRank hologram) and the 9-stage router/compiler (policy, lexical recall,
  cache-aware scoring, dependency/contradiction/negative-knowledge closure, 4-tier packing,
  hashing, Ed25519 signing).
- **Ed25519 signing** (`protocol/signing.py`).
- **Stage B7** (`evals/stage_b7.py`) — model-tier arbitrage (multi-needle retrieval).
- **Research + audit reports** — prior-art studies (plug / provenance / shared memory / path
  viability), citation audit (`RESEARCH-AUDIT.md`), and code/experiment reviews
  (`SIDECAR-REVIEW`, `EPOCHS-STORE-REVIEW`, `RERUN-REVIEW`).

### Changed (honest re-run, 2026-10-17)
- Cold-boot experiment re-run **information-equal** (the raw arm now receives the same
  symbol→module mapping): **348 vs 3,146 tokens (9.0×), 100% vs 25% accuracy** (n=4×3);
  supersedes the confounded 10×/50% claim.
- KV-cache headline restated with baselines: **20.2× vs unshared raw at k=16** (27.95% vs 1.38%
  peak; ~9–12× at finer sampling), **~2× vs a shared raw prefix**; throughput 8.2×.
- Five research-doc corrections applied (arXiv revision numbers, star count, AutoGen status,
  cleared `[UNVERIFIED]` markers) — see `docs/engineering/RESEARCH-AUDIT.md`.

### Fixed (protocol audit)
- RFC 8785 number formatting (ECMAScript form: `1e16`, `1e-5`, `1e-7`), enum serialization,
  `sha256:`-prefixed packet hash, `total_tokens <= hard_total_tokens` and Tier-1 ordering
  validators, deterministic synthesized-block timestamps.
- Kernel: cross-tenant/trust-domain bypass, instruction duplication, no-op closure, oversized
  -cartridge crash, and post-watermark leakage.

### Added (earlier)
- **CPP v1.0 schemas + RFC 8785 canonicalization** (`protocol/`) — immutable
  `ContextPacket`/`ContextBlock` models and deterministic content hashing (ADR-0005).
- **Stage B falsification spike** (`evals/`) — telemetry harness, KV-cache, multi-agent
  fork, compression, quality, negative-knowledge, and cold-boot experiments against a real
  vLLM deployment.
- **Engineering docs** — change log, 5 ADRs, references ledger, and the audit trail.

### Findings (see `docs/engineering/`)
- Intra-agent is a **tie** (prefix caching neutralizes naive broadcast); the value is
  cross-agent, concurrency, and compression.
- **~2× (best-case) to ~9× (realistic) across agents**; **9.5×** for role-divergent children.
- **~1.4–2×** throughput increment from compression and **20× lower KV vs unshared raw
  broadcast** (~2× vs a shared raw prefix) at 16 concurrent agents.
- **~17× compression** on a real package (`httpx`); quality parity at ~5.6×.
- **25 tracked gaps fixed, 1 accepted (GAP-18); GAP-08 undemonstrated and GAP-22
  outstanding** (see `docs/engineering/VERIFICATION-REPORT.md` §A/B).
