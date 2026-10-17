# Implementation Status — Spec vs Code

Maps the normative design (`docs/design/`) to what actually exists in the repo. Keeps the
spec honest about the gap (raised by `VERIFICATION-REPORT.md`).

Legend: ✅ implemented · 🟡 partial · ⬜ pending.

| Area | Spec | Status | Where |
| :--- | :--- | :--- | :--- |
| CPP v1.0 models + frozen schema | ADR-0005 | ✅ | `protocol/models.py` |
| RFC 8785 JCS canonicalization | ADR-0003 | ✅ | `protocol/canonical.py` |
| Ed25519 packet signing | Invariant 10 | ✅ | `protocol/signing.py` |
| `.cfx` cartridge (ast + PageRank hologram) | Module A | ✅ | `kernel/cartridge/` |
| Hard policy engine (trust/RBAC/lifecycle/freshness) | Invariant 2 | ✅ | `kernel/router/policy_engine.py` |
| Lexical recall | Stage 1 | 🟡 | `kernel/router/recall.py` (BM25/embeddings pending) |
| Cache-aware utility scoring | §4 | 🟡 | `kernel/router/scorer.py` (cache discount flag only) |
| Dependency/contradiction/negative-knowledge closure | Stage 4 | ✅ | `kernel/router/dependency_closer.py` |
| 4-tier budget allocation + packing | Stages 5–6 | 🟡 | `kernel/router/packer.py` (MMR diversity pending) |
| U-curve attention placement | Stage 7 | 🟡 | recency anchor emitted; placement not enforced |
| Verify/hash/sign | Stage 8 | ✅ | `kernel/router/compiler.py` |
| Adaptive cartridge + watermark/trust gates | ADR-0002/0004 | ✅ | `kernel/router/` |
| `EPOCH_FOLD` / `COW_FORK` | Module D | ⬜ | pending |
| Shadow Cortex probes | Module B | ⬜ | pending |
| PostgreSQL append-only store + migrations | Db-Storage-Spec | 🟡 | `kernel/migrations/001_*.sql` (driver pending) |
| Sidecar REST API + adapters (raw_llm / vLLM / LMCache) | Plane 2 | ⬜ | pending |
| Deterministic-core tests | — | ✅ | `tests/` |

**Realized core:** deterministic cartridge + policy + scoring + closure + 4-tier packing +
hashing + signing, all tested and type-checked. **Next:** routing into a live adapter,
epoch folding / CoW forking, and the Postgres-backed store.
