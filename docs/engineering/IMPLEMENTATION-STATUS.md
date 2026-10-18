# Implementation Status — Spec vs Code

Maps the normative design (`docs/design/`) to what actually exists in the repo. Keeps the
spec honest about the gap (raised by `VERIFICATION-REPORT.md`).

Legend: ✅ implemented · 🟡 partial · ⬜ pending.

| Area | Spec | Status | Where |
| :--- | :--- | :--- | :--- |
| CPP v1.0 models + frozen schema | ADR-0005 | ✅ | `protocol/models.py` |
| RFC 8785 JCS canonicalization | ADR-0003 | ✅ | `protocol/canonical.py` |
| Ed25519 packet signing | Invariant 10 | ✅ | `protocol/signing.py` |
| Signed epoch chain / receipts (`verify` CLI) | §5 rec. 1–2 | ✅ | `protocol/epoch.py` (Merkle root, membership, compact inclusion proofs, chain verify) |
| `.cfx` cartridge (ast + PageRank hologram) | Module A | ✅ | `kernel/cartridge/` |
| Hard policy engine (trust/RBAC/lifecycle/freshness) | Invariant 2 | ✅ | `kernel/router/policy_engine.py` |
| Recall (BM25 + optional embeddings) | Stage 1 | ✅ | `kernel/router/recall.py` BM25 default; hybrid via optional `embeddings` extra (`CF_RECALL`) |
| Cache-aware utility scoring | §4 | 🟡 | `kernel/router/scorer.py` (cache discount flag only) |
| Dependency/contradiction/negative-knowledge closure | Stage 4 | ✅ | `kernel/router/dependency_closer.py` |
| 4-tier budget allocation + packing | Stages 5–6 | ✅ | λ-MMR diversity in `kernel/router/packer.py` |
| U-curve attention placement | Stage 7 | 🟡 | tail recap + recency anchor rendered; position probe null (n=8) — benefit not demonstrated |
| Verify/hash/sign | Stage 8 | ✅ | `kernel/router/compiler.py` |
| Adaptive cartridge + watermark/trust gates | ADR-0002/0004 | ✅ | `kernel/router/` |
| `EPOCH_FOLD` / `COW_FORK` — memory-state v0 | Module D | ✅ | `kernel/epochs.py` (O(1) fork, private edits, explicit fold + conflict contract) |
| Packet-level `EPOCH_FOLD` / `COW_FORK` | Module D | ✅ | `kernel/mmu/` (k-step fold → Tier 1 facts + `ctx://` handles; fork shares Tier 0/1/2, fresh Tier 3; wired as `cfx fold` / `cfx fork`) |
| `raw_llm` cloud adapters (OpenAI/Anthropic cache mapping) | Module 4 | ✅ | `adapters/raw_llm.py` (v0: Tier-0 breakpoint; Gemini + min-token validation pending) |
| Replayable receipts (`cfx replay` / `cfx seal`) | §5 rec. 2 | ✅ | compile manifests (`compile_log`) + recompile-and-verify + epoch sealing over real traffic (`SEAL.md`) |
| Cache alignment (shared prefix first) | plug rec. #4 | ✅ | `sidecar/render.py` tier0-first; `hints.shared_prefix_hash` == cartridge `kv_prefix_hash` |
| Shadow Cortex probes | Module B | 🟡 | `kernel/router/shadow_cortex.py` (v0: generation + evaluation from existing blocks; git/AST/stacktrace introspection pending) |
| Durable store | Db-Storage-Spec | 🟡 | `kernel/store/` SQLite v0 (blocks + relations, WAL; sidecar reads and writes); Postgres migration + driver pending |
| Sidecar REST API ("the plug") | Plane 2 | 🟡 | `sidecar/` OpenAI-compatible proxy: compile/passthrough, SSE, `cfx` CLI, writable memory (`POST /fabric/blocks`, quarantine=draft), CORS + size cap, `cfx demo`, MCP stdio (`cfx mcp`); LMCache `agent_hint` wiring pending |
| `cfx demo` receipt runner | Track B | ✅ | `sidecar/demo.py` (fleet + negative-knowledge scenarios; GPU-verified, `docs/engineering/DEMO.md`) |
| Agent-hint producer (RFC #52113-shaped) | §4 | 🟡 | `kernel/router/hints.py` + trace `cache_hints`; not yet emitted to an engine (upstream RFC open) |
| Performance budget gate | — | ✅ | `tests/test_perf.py` |
| Deterministic-core tests | — | ✅ | `tests/` |

**Realized core:** deterministic cartridge + policy + BM25/hybrid recall + λ-MMR packing +
closure + hashing + signing, packet-level fold/fork (`cfx fold`/`cfx fork`), replayable +
sealed receipts — plus the product shell: sidecar ("plug", concurrency-hardened),
cloud adapters, MCP surface, writable quarantined memory, `cfx demo` (fleet / repo / mock /
priced), SQLite store, and CI guards (`NUMBERS.md`, perf budget). **Next:** engine-hint wiring
when the vLLM RFC lands, Gemini adapter, Shadow Cortex introspection probes, Postgres
(triggers in `ROADMAP-DECISIONS.md`).

## Order B / B6 claim/code delta decisions

| Item | Code check | Decision | Files changed |
|------|------------|----------|---------------|
| a. MMR | `kernel/router/packer.py:71-126` implements `lam*utility - (1-lam)*max_similarity` and updates penalties against already-selected blocks; this is true MMR. | No code change; docs already describe lambda-MMR correctly. | — |
| b. Purpose trust domains | `kernel/router/policy_engine.py` enforced trust-domain exact match, lifecycle, RBAC, and freshness, but had no purpose gate. `ContextBlock` already carries `allowed_purposes`. | Implement purpose compartments: added optional `purpose` parameter to `apply_policy`; deny when `allowed_purposes` is non-empty and the request purpose is not in it. | `kernel/router/policy_engine.py:23-49`, `tests/test_policy_engine.py` |
| c. Signing | `cfx serve` (`sidecar/server.py`, `sidecar/cli.py`) never passes a signer, so compiled packets are emitted unsigned by default. Docs said "must be signed". | Reword docs from "must"/"signed" to "can be signed"/"signable". No code change; signing remains optional. | `README.md:8`, `README.md:89`, `docs/design/Arch-Spec.md:27`, `docs/engineering/VERIFICATION-REPORT.md:56`, `docs/engineering/SEAL.md:25` |
| d. CoW | Cross-agent sharing in the serve/demo path is prefix sharing via `sidecar/render.py` + `adapters/raw_llm.py`, not KV-page CoW. Packet-level COW exists in `kernel/mmu/cow_forker.py`. | Reword the README "Share" bullet to say "prefix sharing" for the serve path. Leave the real COW_FORK code/docs unchanged. | `README.md:89` |
| e. Capability-claims guard | `scripts/check_docs_numbers.py` only checked numbers, not capability claims. | Add `CAPABILITY_REGISTRY` and `_check_capabilities` scan: docs may assert a phrase only if the named symbol (and optional test) exists. | `scripts/check_docs_numbers.py`, `tests/test_docs_numbers.py` |
