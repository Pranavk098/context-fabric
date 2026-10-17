# Changelog

All notable changes to Context Fabric are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- **Stage C kernel** (`kernel/`) — deterministic `.cfx` cartridge compiler (stdlib `ast`
  extractor + PageRank hologram) and the 9-stage router/compiler (policy, lexical recall,
  cache-aware scoring, dependency/contradiction/negative-knowledge closure, 4-tier packing,
  hashing, Ed25519 signing).
- **Ed25519 signing** (`protocol/signing.py`).
- **Stage B7** (`evals/stage_b7.py`) — model-tier arbitrage (multi-needle retrieval).
- **Research/audit reports** — competitive landscape, SOTA & threats, protocol review,
  kernel review, verification report.

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
- **~1.4–2×** throughput and **20× lower KV** at 16 concurrent agents.
- **~17× compression** on a real package (`httpx`); quality parity at ~5.6×.
- **All 27 tracked gaps fixed or accepted; none rescoped** (GAP-08 arbitrage remains open).
