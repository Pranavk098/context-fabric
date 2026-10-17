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
| Lexical recall | Stage 1 | 🟡 | `kernel/router/recall.py` (BM25/embeddings pending) |
| Cache-aware utility scoring | §4 | 🟡 | `kernel/router/scorer.py` (cache discount flag only) |
| Dependency/contradiction/negative-knowledge closure | Stage 4 | ✅ | `kernel/router/dependency_closer.py` |
| 4-tier budget allocation + packing | Stages 5–6 | 🟡 | `kernel/router/packer.py` (MMR diversity pending) |
| U-curve attention placement | Stage 7 | 🟡 | recency anchor emitted; placement not enforced |
| Verify/hash/sign | Stage 8 | ✅ | `kernel/router/compiler.py` |
| Adaptive cartridge + watermark/trust gates | ADR-0002/0004 | ✅ | `kernel/router/` |
| `EPOCH_FOLD` / `COW_FORK` (memory-state v0) | Module D | ✅ | `kernel/epochs.py` (O(1) fork, private edits, explicit fold + conflict contract; packet-level k-step tool-output fold per Build-Plan §44 pending) |
| Shadow Cortex probes | Module B | ⬜ | pending |
| Durable store | Db-Storage-Spec | 🟡 | `kernel/store/` SQLite v0 (+ sidecar `.db` loading); Postgres migration + driver pending |
| Sidecar REST API ("the plug") | Plane 2 | 🟡 | `sidecar/` OpenAI-compatible proxy (compile/passthrough, SSE streaming); raw_llm/LMCache `agent_hint` adapter pending |
| Deterministic-core tests | — | ✅ | `tests/` |

**Realized core:** deterministic cartridge + policy + scoring + closure + 4-tier packing +
hashing + signing, plus the v0 product shell — OpenAI-compatible sidecar ("plug"), signed
epoch receipts ("seal"), memory-state CoW/fold ("shared copy"), and a SQLite store ("filing
cabinet") — all tested and type-checked. **Next:** LMCache/`agent_hint` integration, thin MCP
surface, packet-level epoch folding, and the Postgres-backed store.
