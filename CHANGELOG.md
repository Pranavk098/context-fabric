# Changelog

All notable changes to Context Fabric are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added (2026-10-24)
- Claude Code plugin alpha (`cfxd`): resident local daemon with brief, carry and fold (fold off by default),
  `stats`, `audit`, `replay`, `doctor`; Windows, macOS and Linux CI; `cfxd` ships in the wheel with a `cfxd` command.
- `docs/INSTALL-ALPHA.md`, `SECURITY.md`, alpha-feedback issue template; README rewritten for the plugin era.

### Fixed
- cfxd's own loopback calls no longer go through an HTTP proxy set in the environment.
- Tests are hermetic with respect to proxy environment variables.
- CI workflows use read-only token permissions and commit-pinned actions; `docker-compose.yml` publishes the
  inference port on 127.0.0.1 only.

### Security hardening (G2/G3, 2026-10-19)
- **CORS opt-in allow-list** — `cfx serve` no longer emits permissive
  `Access-Control-Allow-Origin: *`; CORS is off by default and any request with a browser
  `Origin` not in `--cors-origin <origin>` (repeatable) / `CF_CORS_ORIGINS` is rejected `403`.
  Non-browser agent tools (no `Origin` header) are unaffected.
- **Server-side trust only** — `POST /fabric/blocks` and MCP `memory_add` ignore
  client-supplied `"trusted"`; blocks land `draft` unless the operator starts with `--trusted`
  (`CF_TRUST_BLOCKS`) or runs `cfx add --trusted` out-of-band.
- **Bounded uploads** — single valid `Content-Length` required (`411`/`400`/`413`); bodies over
  `CF_MAX_BODY_BYTES` (33,554,432) are drained in bounded chunks before closing (`AU2-M1`).
- **Seal key off argv** — `cfx seal` reads the Ed25519 seed from `--key-file` (env
  `CF_SEAL_KEY_FILE`) or `CF_SEAL_KEY`, never a raw CLI flag (`AU2-M2`).
- Store multi-statement write lock and server connection-pool close (`AU3-M4`, `AU3-L9`).

### Added (v0.2 release candidate)
- **Observation folding & folded-session replay** — `cfx serve --obs-fold` (default OFF) masks
  older `role:"tool"` messages into reversible `ctx://obs/<id>` stubs, keeps the last `N`
  verbatim, and advances the fold boundary every `K` outputs for ReFold-style prefix-cache
  alignment; `cfx expand` (and MCP `expand`) recover the stored output, and `cfx replay-fold`
  deterministically re-applies fold events and verifies `seen_hash`
  (`docs/engineering/OBS-FOLD.md`).
- **KvHintsEnvelope adapter** — `cfx serve --kv-hints` (default OFF) maps `packet_cache_hints`
  into the vLLM PR #53423 schema; off by default so forwarded requests stay byte-identical
  (`docs/engineering/AGENT-HINTS.md`).
- **LangGraph adapter** — `adapters/langgraph/` (`compile_node_packet` / `node_model_input`)
  turns a LangGraph state dict into a compiled packet on the exact kernel compile path, with an
  offline example and semantic-parity test (`docs/engineering/ADAPTERS.md`).
- **Agent-tool compatibility** — compiled-objective extraction skips injected tool-error and
  output-limit reminder messages (S4); bare `GET /models` proxied alongside `GET /v1/models`
  (S5); purpose compartments (`X-Fabric-Purpose`) wired end-to-end through `cfx serve`.
- **B8 live-test harness** — `--window-cap` (400 `context_length_exceeded`), model allow-list +
  atomic daily token ledger (`--b8-ledger-dir` / `--b8-daily-cap`) for live SWE-bench runs
  (`sidecar/b8.py`).
- **Store auto-migration** — existing `.db` stores migrate `compile_log` in place (adds the
  `purpose` column) and create `fold_log` / `fold_requests` automatically.

### Results — negative & exploratory (not claims)
- **Pilot on mechanical memory** — **13.62× fewer billed prompt tokens at 5/6 parity**
  (193,366 vs 14,193); on a warm prefix cache raw pays 182 uncached vs 11,857 for compile, so
  the win is window fit and KV, not prefill (`docs/engineering/PILOT-MECH-RESULT.md`).
- **B4 KILL** — divergent-role compile lost to one shared, prefix-cached raw prompt on quality
  (21/36 vs 29/36) and uncached prefill (`docs/engineering/B4-RESULT.md`).
- **B4 v3 MIXED (KILL under corrected scoring)** — when the repo exceeds the window, compile is
  cheapest (5.66× billed reduction) but plain BM25 chunks answered more questions
  (`docs/engineering/B4-V3-RESULT.md`).
- **A4 concurrency** — compile holds less peak KV at every N (10.8×→1.9×) and wins p99 through
  N=16, but raw's shared prefix wins throughput and p99 at N=32
  (`docs/engineering/A4-CONCURRENCY-RESULT.md`).
- **Skim / skim-model FAIL** — neither deterministic nor model-based question-guided skimmers
  beat whole-chunk BM25 (`docs/engineering/SKIM.md`, `SKIM-MODEL.md`).
- **B2 cost model** — modeled list-price cost vs native prefix caching (`docs/engineering/COSTS.md`).
- **B8 SWE-bench live pilot is in progress** — no task-resolution claim is made
  (`docs/engineering/B8-PREREGISTRATION-v2.md`).

### Added
- **Packet-level fold/fork** — `kernel/mmu/`: k-step `EPOCH_FOLD` (raw tool outputs → Tier 1
  facts + `ctx://` handles, append-only, deterministic) and `COW_FORK` (children share Tier
  0/1/2, fresh Tier 3, lineage via `parent_packet_id`); wired into the CLI as `cfx fold` /
  `cfx fork` (`sidecar/foldfork.py`) — the live HTTP request-path observation folding uses
  `cfx serve --obs-fold` (`sidecar/obsfold.py`), a separate implementation
  (`docs/engineering/MMU-FOLD-FORK.md`).
- **Cache-alignment fix** — the renderer now puts the shared Tier 0 cartridge first (per-agent
  instruction second), so cross-agent KV prefix sharing is possible; byte-identity tests +
  `hints.shared_prefix_hash` (`AGENT-HINTS.md`).
- **Replayable receipts** — compile manifests (`compile_log`), `cfx replay` (recompiles from the
  recorded block set; detects store drift) and `cfx seal` (signed epoch over real traffic with
  stack metadata) (`docs/engineering/SEAL.md`).
- **Merkle inclusion proofs** — `merkle_proof` / `verify_merkle_proof` (`protocol/epoch.py`).
- **Recall upgrade** — BM25 (k1=1.2/b=0.75) replaces naive lexical overlap; optional embeddings
  extra (`[embeddings]`, fastembed/bge-small) with `CF_RECALL=bm25|hybrid|embeddings`. Benchmark:
  top-1 0.00→0.85, MRR 0.29→0.86 vs the old baseline (`docs/engineering/RECALL-BENCH.md`).
- **λ-MMR packing + U-curve recap** — diversity-aware selection in the packer (−13.4% redundancy
  vs greedy; coverage trade-off 5/6 vs 6/6 — corrected by audit) and a KEY CONSTRAINTS tail
  recap; position probe was a null, documented (`docs/engineering/PACKING-QUALITY.md`).
- **MCP stdio server** — `cfx mcp` (search / receipt verify / quarantine-aware add; spec
  2025-11-25) (`docs/engineering/MCP.md`).
- **Perf budget gate + agent-hint producer** — `tests/test_perf.py` (compile <250 ms at 1000
  blocks; measured ~61 ms post-MMR); `kernel/router/hints.py` (RFC #52113-shaped, trace-only)
  (`docs/engineering/PERFORMANCE.md`, `AGENT-HINTS.md`).
- **The plug** — OpenAI-compatible sidecar proxy (`sidecar/`): compile/passthrough modes, SSE
  streaming, stats; ~10-minute setup (`docs/engineering/SIDECAR.md`).
- **`cfx` CLI + writable memory** — `cfx serve|add|verify`; `POST /fabric/blocks` with
  quarantine-by-default (`draft` lifecycle, promote at write time via `--trusted`), plus
  sidecar hardening: CORS, `CF_MAX_BODY_BYTES` cap, HTTP/1.1 keep-alive, SSE abort marking,
  streaming usage capture, `estimated_input_cost_usd` in `/fabric/stats`.
- **Relations in the durable store** — `kernel/store/` relations table + append/load/ingest
  APIs + CLI; contradiction/dependency closure verified end-to-end from a DB-backed fabric
  (`docs/engineering/STORE.md`).
- **`cfx demo` — the receipt demo** (`sidecar/demo.py`): one command runs a 6-agent fleet in two
  arms (best-case broadcast vs compile) against any OpenAI-compatible endpoint and prints a
  receipt (tokens, accuracy, cache, KV, cost if priced); negative-knowledge scenario with
  control/treated traps and v1+v2 scorers. GPU-verified (regenerated post-audit set): **3.17×
  fewer prompt tokens at equal accuracy**, wall 2.80 s → 2.08 s; receipts now embed raw
  responses and prompt hashes (`docs/engineering/DEMO.md`; audit: `DEMO-REVIEW.md`).
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
  (`SIDECAR-REVIEW`, `EPOCHS-STORE-REVIEW`, `RERUN-REVIEW`, `TRACK-A-REVIEW`).

### Changed (honest re-run, 2026-10-17)
- Cold-boot experiment re-run **information-equal** (the raw arm now receives the same
  symbol→module mapping): **348 vs 3,146 tokens (9.0×), 100% vs 25% accuracy** (n=4×3);
  supersedes the confounded 10×/50% claim.
- KV-cache headline restated with baselines: **20.2× vs unshared raw at k=16** (27.95% vs 1.38%
  peak; ~9–12× at finer sampling), **~2× vs a shared raw prefix**; throughput **8.23×**
  (43.25 vs 5.26 req/s).
- Five research-doc corrections applied (arXiv revision numbers, star count, AutoGen status,
  cleared `[UNVERIFIED]` markers) — see `docs/engineering/RESEARCH-AUDIT.md`.

### Fixed (Track B)
- **Recency anchor now rendered** — the packer built prohibitions + output contract but
  `sidecar/render.py` never delivered them; negative knowledge now lands at the tail of the
  injected context (U-curve position). Found by the demo's negative-knowledge scenario,
  verified in the rerun (`docs/engineering/DEMO.md` §Results).

### Fixed (gap-wave audits)
- MCP stdin crashes on malformed UTF-8 and lone-surrogate epoch payloads (S1).
- `pack(mmr_lambda=1.0)` control-fidelity bug — the packing bench's greedy arm had measured the
  wrong algorithm; corrected numbers: −13.4% redundancy, coverage 5/6 vs 6/6 (S2).
- Render recap duplicated long negative-knowledge blocks already covered by prohibitions;
  hybrid scoring dropped a legitimate block with id `"idf"`; stale `PERFORMANCE.md` numbers.
  Full audits: `docs/engineering/GAP-WAVE-REVIEW.md`, `GAP-WAVE-MEASUREMENTS.md`.

### Fixed (Tracks C+D audit)
- Verifier fail-closed guards: malformed `verify_merkle_proof` input, empty-epoch `verify_epoch`,
  mixed naive/aware `verify_chain` (S1); manifest/fabric write race and fold lifecycle
  promotion (S2); doc falsehoods corrected (`docs/engineering/TRACK-CD-REVIEW.md`).

### Added (scrum wave, 2026-10-18)
- **Real-repo pilot** — pre-registered criteria, real `httpx` @ `b5addb64`, 32k-token contexts:
  **35.87× fewer prompt tokens at accuracy parity (5/6 vs 5/6)**, all criteria PASS
  (`PILOT.md`, `PILOT-PREREGISTRATION.md`).
- **Engine portability** — Ollama reproduces vLLM exactly (16/16 byte-identical prompts;
  3.17× holds); 4k-context truncation gotcha documented (`ENGINE-MATRIX.md`).
- **Cloud adapters v0** — `adapters/raw_llm.py`: OpenAI/Anthropic request shapes with
  `cache_control` on the shared prefix (`ADAPTERS.md`).
- **`cfx demo --repo`** — the receipt on any local Python repo (auto objectives from real
  symbols) + `--mock` (no GPU) + `--price` cost receipts with provider presets
  (`DEMO.md`, `COSTS.md`).
- **MMU wired** — `cfx fold` / `cfx fork`; **Shadow Cortex v0** (`SHADOW-CORTEX.md`);
  scorer v3 consolidated with a fresh negative rerun (3B: control 4/4 bad → treated 0/4 under
  v1 and v3; `NEGATIVE-SCORER.md`).
- **Guards** — canonical `NUMBERS.md` registry + `scripts/check_docs_numbers.py` in CI;
  offline sidecar concurrency bench (`PERFORMANCE.md`).

### Fixed (scrum wave)
- Sidecar backlog overflow (`request_queue_size` 5 → 256): 23 errors at N=64 → 0; p99
  2057 ms → 166 ms (`SCRUM-REVIEW.md`, `SCRUM-MEASUREMENTS.md`).
- `cfx demo --repo`: unreadable-file crash and symlink path escape (S1/S2); fold/fork audit
  S2s (repeat-`packet_id` warning; fork Tier-1 RBAC filter).

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
- **~2× (best-case) to ~9× (realistic) across agents**; **9.5×** for role-divergent children
  (unequal-information caveat: VERIFICATION-REPORT N2).
- **~1.4×** throughput increment from compression (on top of sharing) and **20.2× lower KV
  vs unshared raw broadcast** (~2× vs a shared raw prefix) at 16 concurrent agents.
- **~17× compression** on a real package (`httpx`); parity on a synthetic extractive
  lookup/comparison set at ~5.6× (N7 caveats).
- **25 tracked gaps fixed, 1 accepted (GAP-18); GAP-08 undemonstrated and GAP-22
  outstanding** (see `docs/engineering/VERIFICATION-REPORT.md` §A/B).
