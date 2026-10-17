# Changelog

All notable changes to Context Fabric are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
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
