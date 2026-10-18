# Implementation Status — Spec vs Code

Maps the normative design (`docs/design/`) to what actually exists in the repo. Keeps the
spec honest about the gap (raised by `VERIFICATION-REPORT.md`).

Legend: ✅ implemented · 🟡 partial · ⬜ pending.

| Area | Spec | Status | Where |
| :--- | :--- | :--- | :--- |
| CPP v1.0 models + frozen schema | ADR-0005 | ✅ | `protocol/models.py` |
| RFC 8785 JCS canonicalization | ADR-0003 | ✅ | `protocol/canonical.py` |
| Ed25519 packet signing | Invariant 10 | ✅ | `protocol/signing.py` |
| Signed epoch chain / receipts (`verify` CLI) | §5 rec. 1–2 | ✅ | `protocol/epoch.py` (v0: correct Merkle root + membership check; compact inclusion proofs pending) |
| `.cfx` cartridge (ast + PageRank hologram) | Module A | ✅ | `kernel/cartridge/` |
| Hard policy engine (trust/RBAC/lifecycle/freshness) | Invariant 2 | ✅ | `kernel/router/policy_engine.py` |
| Recall (BM25 + optional embeddings) | Stage 1 | ✅ | `kernel/router/recall.py` BM25 default; hybrid via optional `embeddings` extra (`CF_RECALL`) |
| Cache-aware utility scoring | §4 | 🟡 | `kernel/router/scorer.py` (cache discount flag only) |
| Dependency/contradiction/negative-knowledge closure | Stage 4 | ✅ | `kernel/router/dependency_closer.py` |
| 4-tier budget allocation + packing | Stages 5–6 | ✅ | λ-MMR diversity in `kernel/router/packer.py` |
| U-curve attention placement | Stage 7 | 🟡 | tail recap + recency anchor rendered; position probe null (n=8) — benefit not demonstrated |
| Verify/hash/sign | Stage 8 | ✅ | `kernel/router/compiler.py` |
| Adaptive cartridge + watermark/trust gates | ADR-0002/0004 | ✅ | `kernel/router/` |
| `EPOCH_FOLD` / `COW_FORK` (memory-state v0) | Module D | ✅ | `kernel/epochs.py` (O(1) fork, private edits, explicit fold + conflict contract; packet-level k-step tool-output fold per Build-Plan §44 pending) |
| Shadow Cortex probes | Module B | ⬜ | pending |
| Durable store | Db-Storage-Spec | 🟡 | `kernel/store/` SQLite v0 (blocks + relations, WAL; sidecar reads and writes); Postgres migration + driver pending |
| Sidecar REST API ("the plug") | Plane 2 | 🟡 | `sidecar/` OpenAI-compatible proxy: compile/passthrough, SSE, `cfx` CLI, writable memory (`POST /fabric/blocks`, quarantine=draft), CORS + size cap, `cfx demo`, MCP stdio (`cfx mcp`); LMCache `agent_hint` wiring pending |
| `cfx demo` receipt runner | Track B | ✅ | `sidecar/demo.py` (fleet + negative-knowledge scenarios; GPU-verified, `docs/engineering/DEMO.md`) |
| Agent-hint producer (RFC #52113-shaped) | §4 | 🟡 | `kernel/router/hints.py` + trace `cache_hints`; not yet emitted to an engine (upstream RFC open) |
| Performance budget gate | — | ✅ | `tests/test_perf.py` |
| Deterministic-core tests | — | ✅ | `tests/` |

**Realized core:** deterministic cartridge + policy + BM25/hybrid recall + λ-MMR packing +
closure + hashing + signing, plus the product shell — sidecar ("plug"), MCP surface, signed
epoch receipts ("seal"), memory-state CoW/fold ("shared copy"), SQLite store ("filing
cabinet"), demo receipts, and a perf budget gate. **Next:** Track C (packet-level
`EPOCH_FOLD`/`COW_FORK`, cache alignment) and Track D (replay CLI, Merkle proofs, stack
metadata); then Postgres + engine wiring. Deliberate deferrals: `ROADMAP-DECISIONS.md`.
