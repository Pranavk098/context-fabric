# Release Candidate Notes — Context Fabric v0.2 (Track T4)

**Status:** Release-candidate preparation (`v0.2.0-rc`, docs and verification only).
`pyproject.toml` remains at `version = "0.1.0"` and no git tag is cut until the live B8 v3
pilot finishes (`pyproject.toml` and `uv.lock` are frozen during the pilot window).

---

## 1. What v0.2 ships

### Sidecar: reversible observation folding & folded-session replay
- **Request-path observation folding (`cfx serve --obs-fold`, default OFF)** —
  `sidecar/obsfold.py` masks older `role: "tool"` messages into content-addressed stubs
  (`[ctx://obs/<sha256-12> folded: <tool>, <bytes> bytes, <first line>]`) while keeping the
  most recent `N` observations verbatim (`--obs-n`, default `3`). ReFold-style chunking
  (`--obs-k`, default `3`) advances the fold boundary only every `k` new tool outputs so
  consecutive prompts remain append-only between fold events and preserve upstream prefix
  caches. An optional token guard (`--obs-max-tokens`) folds additional oldest tool messages
  until the request fits (`docs/engineering/OBS-FOLD.md`).
- **Lossless expansion (`cfx expand` + MCP `expand`)** — when `--obs-store <obs.db>` (env
  `CF_OBS_STORE`) is configured, full tool output is stored as an `observation` block
  (`obs_<id>`) alongside an append-only `fold_log` receipt (`seen_hash` over the RFC 8785
  canonical folded message list). Operators and agents can recover the verbatim output via
  `cfx expand --store <obs.db> ctx://obs/<id>` or the MCP `expand` tool.
- **End-to-end folded-session replay (`cfx replay-fold`)** — `sidecar/replay.py` and the
  `fold_requests` SQLite table record each unfolded request together with its fold events.
  `cfx replay-fold --store <obs.db> [--session <id>]` deterministically re-applies the
  recorded fold events and verifies `seen_hash` byte-equality (`FOLD REPLAY OK`); adding
  `--expand` reconstructs the original unfolded request and verifies every `obs_<id>` block's
  `content_hash` (`docs/engineering/OBS-FOLD.md`).
- **Offline trajectory replay lab (`evals/replay_lab.py` + `sidecar/obsfold_v2.py`)** —
  exploratory offline analyzer that replays recorded `mini-swe-agent` trajectories under
  `none`, `obsfold`, and content-aware `obsfold_v2` policies to inspect token growth, window
  overflows, and shared-prefix length (`docs/engineering/REPLAY-LAB.md`).
- **Agent-tool compatibility fixes** — compile objective extraction (`_objective` in
  `sidecar/server.py`) skips injected tool-error and output-limit reminder user messages so
  failed turns do not churn the compiled packet objective (`S4`); bare `GET /models` is
  proxied alongside `GET /v1/models` (`S5`); purpose compartments (`X-Fabric-Purpose`
  header → `compile_packet(..., purpose=...)`) are wired end-to-end through `cfx serve`,
  `CompileManifest`, and `cfx replay`.

### Receipts, seal, and packet-level MMU CLI
- **Compile manifests & packet replay (`cfx replay`)** — every compile against a SQLite
  fabric (`--fabric <path.db>`) logs a `CompileManifest` in `compile_log` (recording scope,
  recipient, objective, stage, explicit `as_of`, watermark, budget base, purpose, input block
  hashes, and `CF_MODEL`/`CF_ENGINE` metadata). `cfx replay --store <db> --packet-id <cpk_...>`
  recompiles from the recorded block set, detects store drift, and verifies packet-hash
  equality (`docs/engineering/SEAL.md`).
- **Signed epoch sealing (`cfx seal` & `cfx verify`)** — commits the store's `compile_log`
  into a signed, hash-chained `Epoch` (`protocol/epoch.py`) with sorted Merkle leaves,
  O(log n) inclusion proofs (`merkle_proof` / `verify_merkle_proof`), and `prev_epoch_hash`
  linkage. Verified offline via `cfx verify <epochs.json> --pubkey <hex>`.
- **Packet-level MMU CLI (`cfx fold` & `cfx fork`)** — `sidecar/foldfork.py` exposes
  `kernel/mmu/` on the CLI: `cfx fold --store <db> [--k 3]` folds raw tool-output events into
  Tier-1 facts + `ctx://` handles, and `cfx fork --store <db> --packet-id <cpk_...> --agent ...`
  spawns an RBAC-filtered Copy-on-Write child packet (`docs/engineering/MMU-FOLD-FORK.md`).

### Security hardening (Audit G2 / G3)
- **CORS opt-in allow-list (`AU2-H1`)** — `cfx serve` binds `127.0.0.1` and disables CORS by
  default. Any request carrying a browser `Origin` header is rejected `403` unless that
  origin is explicitly allow-listed with repeatable `--cors-origin <origin>` (or
  comma-separated `CF_CORS_ORIGINS`). Non-browser agent tools (which send no `Origin` header)
  pass through untouched.
- **Server-side trust only (`AU2-H2`)** — `POST /fabric/blocks` and MCP `memory_add` ignore
  client-supplied `"trusted"` fields. Blocks land quarantined as `lifecycle_status="draft"`
  unless the operator starts `cfx serve --trusted` (env `CF_TRUST_BLOCKS`), `cfx mcp --trusted`,
  or runs `cfx add --trusted` out-of-band.
- **Bounded upload framing & 413 drain (`AU2-M1`)** — requests require a single valid
  `Content-Length` (`411` on missing or `Transfer-Encoding: chunked`, `400` on malformed,
  `413` above `CF_MAX_BODY_BYTES`, default 33,554,432 bytes). Oversized bodies are drained in
  bounded chunks (`_MAX_DRAIN_BYTES = 8 MiB`, `_DRAIN_TIMEOUT = 5.0 s`) before closing the
  socket so a stalled upload cannot pin a handler thread or race a TCP RST on Windows.
- **Signing key off `argv` (`AU2-M2`)** — `cfx seal` reads the 32-byte Ed25519 seed hex from
  `--key-file <path>` (env `CF_SEAL_KEY_FILE`) or `CF_SEAL_KEY`, never from a raw CLI flag,
  keeping private keys out of `ps`/shell history.
- **Store & client concurrency hygiene (`AU3-M4`, `AU3-L9`)** — `SqliteStore` serializes all
  multi-statement writes under an internal re-entrant lock (`self._write_lock`), and
  `_Server.server_close()` closes its shared `httpx.Client` connection pool.

### Framework & engine adapters
- **LangGraph adapter (`adapters/langgraph/`, optional `langgraph` extra)** —
  `compile_node_packet` and `node_model_input` turn a LangGraph state dict into a compiled
  `ContextPacket` and OpenAI-shaped system message on the exact kernel compile path, with an
  offline 3-node example (`python -m adapters.langgraph.example`) and semantic-parity test
  (`tests/test_langgraph_parity.py`) (`docs/engineering/ADAPTERS.md`).
- **vLLM `KvHintsEnvelope` adapter (`kernel/router/kv_hints_envelope.py`, default OFF)** —
  maps `packet_cache_hints(packet)` into the merged vLLM PR #53423 `KvHintsEnvelope` schema
  (`protocol_version: "0.1"`, content-derived `message_id`, action
  `context_fabric.cache_hint`). Opt-in on the sidecar via `cfx serve --kv-hints` (env
  `CF_KV_HINTS`); off by default so forwarded requests stay byte-identical
  (`docs/engineering/AGENT-HINTS.md`).

### Reproducibility & evaluation guards
- **`cfx repro` & canonical numbers guard** — `cfx repro` and `scripts/check_docs_numbers.py`
  verify every strict entry in `docs/engineering/NUMBERS.md` from its committed JSON artifact
  under `evals/reports/`, enforce a named `baseline` per strict entry, block stale numbers,
  and verify `CAPABILITY_REGISTRY` code+test backing.
- **Sub-agent window cap & daily token ledger** — `cfx serve --window-cap <N>` (env
  `CF_B8_WINDOW_CAP`) returns `400 context_length_exceeded` when estimated input +
  `max_tokens` exceeds `N`; `--b8-ledger-dir` / `--b8-daily-cap` enforce model allow-listing
  and atomic daily token caps for live benchmarks (`sidecar/b8.py`).

---

## 2. What v0.2 explicitly does NOT claim

Every negative, mixed, or exploratory result is part of the release record and must not be
overstated:

1. **Per-role compiling does NOT beat prefix caching when the repository fits in context
   (`B4 KILL`).** In pre-registered B4 (`httpx`, Qwen2.5-1.5B, 4 roles, 36 requests/arm),
   compiled packets lost to a single shared, prefix-cached raw prompt on both answer quality
   (`21/36 vs 29/36`) and uncached prefill tokens (`13,860` vs `8,871`). See
   [`B4-RESULT.md`](B4-RESULT.md) (`evals/reports/b4_divergent_20261018_145937.json`).
2. **When the repository does not fit the window, compiling is cheapest on tokens/KV but NOT
   most accurate (`B4 v3 MIXED` / `KILL` under corrected scoring).** In pre-registered B4 v3
   (full `httpx` ~64.8k tokens vs a 16k window, 4k budget), compile reduced billed prompt
   tokens by `5.66x` vs truncation and used the fewest uncached tokens (`46,907` vs `65,908`
   truncate and `75,212` retrieval) and lowest peak KV, but plain BM25 over AST chunks (`R`)
   answered more questions on both 1.5B (`7` vs `9` frozen; `7` vs `11` with punctuation-trim
   scorer fix → `KILL`) and 0.5B (`3` vs `5` frozen; `3` vs `6` corrected → `KILL`). See
   [`B4-V3-RESULT.md`](B4-V3-RESULT.md) (`evals/reports/b4_v3_20261018_202752.json`).
3. **Question-guided code skimmers (deterministic and model-based) do NOT beat BM25 chunks
   (`SKIM` / `SKIM-MODEL` exploratory FAIL).** Neither deterministic AST+line BM25 skimming
   (`kernel/router/skim.py`) nor the two local-GPU model skimmers (`SKIM-PPL` contrastive
   perplexity on Qwen2.5-0.5B and `SKIM-PRUNER` on SWE-Pruner 634M in
   `kernel/router/skim_model.py`) beat whole-chunk BM25 (`R`) on dev-set fact recall at
   1k/2k/4k budgets, and the model skimmers add ~3.2–4.5 s cold latency per hand-off. Both
   stay behind default-off flags. See [`SKIM.md`](SKIM.md) and [`SKIM-MODEL.md`](SKIM-MODEL.md).
4. **Warm prefix cache beats mechanical compilation on uncached prefill tokens.** In the
   mechanical-memory real-repo pilot (`PILOT-MECH-RESULT.md`), compile reduces billed prompt
   tokens by `13.62x` at `5/6` parity (`193,366` vs `14,193`), but on a warm prefix cache the
   shared raw prompt pays only `182` uncached tokens vs `11,857` for compile. Compile's win
   is context window fit, peak KV footprint, and cold/TTL-miss cost — not warm-cache prefill.
   See [`PILOT-MECH-RESULT.md`](PILOT-MECH-RESULT.md) and [`COSTS.md`](COSTS.md).
5. **High-concurrency KV savings trade off against scheduler queueing at `N=32` (`A4`).**
   In pre-registered A4 (`A4-CONCURRENCY-RESULT.md`), compile holds less peak KV than raw at
   every concurrency level (`10.8×` at `N=1` down to `1.9x` at `N=32`, with `10.9x` fewer
   prompt tokens per request) and wins throughput/p99 through `N=16`, but at `N=32` raw's
   single shared prefix wins throughput (`5.44` vs `4.95 req/s`) and p99 latency (`5.88 s` vs
   `6.45 s`). See [`A4-CONCURRENCY-RESULT.md`](A4-CONCURRENCY-RESULT.md).
6. **No savings or accuracy claims for `--obs-fold`, `obsfold_v2`, or the LangGraph adapter.**
   Observation folding (`OBS-FOLD.md`), trajectory replay (`REPLAY-LAB.md`), and the
   LangGraph adapter (`ADAPTERS.md`) ship with deterministic correctness and replay/parity
   tests, not token-savings or task-resolution headlines.
7. **`cfx serve` does not yet wire Tier-0 cartridges or store relations on the HTTP path.**
   `compile_packet` mounts a Tier-0 cartridge and relation closure when called from Python;
   the HTTP server passes neither (`shared_prefix == ""`). The `13.62x` and `3.17x` figures
   come from Python harness calls (`IMPLEMENTATION-STATUS.md`).
8. **`--kv-hints` is forward-compatible transport plumbing only.** vLLM 0.31.0's OpenAI REST
   entrypoint does not parse `kv_hints` and no built-in scheduler action consumes it yet
   (`AGENT-HINTS.md`).
9. **Receipts prove context bytes, never model output reproduction.** `cfx replay`,
   `cfx replay-fold`, and `cfx verify` prove the exact packet or folded request bytes
   delivered, not why the model answered (`SEAL.md`).
10. **B8 SWE-bench live pilot is in progress.** No SWE-bench task-resolution claim is made in
    this release candidate (`B8-PREREGISTRATION-v2.md`).

---

## 3. Measured numbers v0.2 may cite

Release notes, README, and external summaries may cite **only** entries registered in
[`NUMBERS.md`](NUMBERS.md), always paired with their stated baseline and caveat:

| Registry ID | Display | Kind | Baseline & caveat | Source artifact |
| :--- | :--- | :--- | :--- | :--- |
| `pilot_mech_prompt_token_ratio` | `13.62x` | strict | vs full 32,259-token `httpx` raw bundle (strongest naive); mechanical memory, `5/6` vs `5/6` parity | `evals/reports/pilot_20261018_151640.json` |
| `pilot_mech_raw_uncached` | `182` | strict | raw arm uncached tokens on a warm prefix cache (`193,366 − 193,184`) | `evals/reports/pilot_20261018_151640.json` |
| `pilot_mech_compile_uncached` | `11,857` | strict | compile arm uncached tokens (`14,193 − 2,336`); warm cache favors raw on prefill | `evals/reports/pilot_20261018_151640.json` |
| `pilot_prompt_token_ratio` | `35.87x` | strict | superseded hand-written blocks (`193,366` raw vs `5,391` compile); always qualify with `13.62x` mechanical | `evals/reports/pilot_20261018_013151.json` |
| `demo_prompt_token_ratio` | `3.17x` | strict | vs broadcast arm (`44,854` vs `14,170` tokens, `30,684` saved) at `6/6` parity on synthetic fixture | `evals/reports/demo_fleet_20261017_231929.json` |
| `kv_peak_ratio` / `kv_peak_percentages` | `20.2x` (`27.95% vs 1.38%`) | strict | `k=16` concurrent agents vs *unshared* raw (`B0_divergent`); ~`2x` vs a shared raw prefix | `evals/reports/stage_b5_20261017_124856.json` |
| `throughput_ratio` / `throughput_detail` | `8.23x` (`43.25 vs 5.26 req/s`) | strict | `k=16` vs *unshared* raw (`B0_divergent`); shared raw prefix alone reaches ~35 req/s | `evals/reports/stage_b5_20261017_124856.json` |
| `coldboot_tokens` / `coldboot_ratio` / `coldboot_accuracy` | `348 vs 3,146` (`9.0x`), `100% vs 25%` | strict | information-equal re-run (`n=4×3`, Qwen2.5-1.5B) | `evals/reports/audit_b6_20261017_124755.json` |
| `recall_top1_delta` | `0.00 → 0.85` | strict | BM25 top-1 minus pre-BM25 lexical baseline on synthetic recall bench | `evals/reports/recall_bench_20261018T061752Z.json` |
| `packing_redundancy` | `-13.4%` | strict | λ-MMR pairwise Jaccard reduction vs greedy (`mmr_lambda=1.0`); coverage trade-off `5/6` vs `6/6` | `evals/reports/packing_bench_20261017_231759.json` |
| `quality_parity_ratio` | `~5.6x` | strict | B6 lossless compile vs raw-full (`408 / 73` tokens) at 100% accuracy (N7 caveats) | `evals/reports/audit_b6_20261016_173804.json` |
| `httpx_compression` | `~17x` | display-only | extractive `.cfx` compression on `httpx` (`64,066 → 3,806` tokens; no stored JSON report) | — (`ENGINEERING-LOG.md`) |
| `b4_kill_quality` | `21/36 vs 29/36` | display-only | B4 divergent-role compile vs prefix-cache-only baseline (**KILL**) | `evals/reports/b4_divergent_20261018_145937.json` |
| `b4_v3_billed_ratio` | `5.66x` | display-only | B4 v3 compile billed-token reduction vs truncation (**MIXED** / **KILL** under fixed scorer) | `evals/reports/b4_v3_20261018_202752.json` |
| `a4_kv_peak_n32` / `a4_token_ratio` | `1.9x` / `10.9x` | display-only | A4 concurrency peak-KV reduction at `N=32` and per-request prompt-token reduction vs shared raw | `evals/reports/a4_compile_20261019_160730.json` |
| `cost_*` (12 entries) | see [`COSTS.md`](COSTS.md) | display-only | modeled list-price cost (`openai:gpt-4o`, `anthropic:sonnet`, warm vs TTL miss) | `docs/engineering/COSTS.md` |
| `test_count` | `536` | display-only | `uv run python -m pytest --collect-only -q` (`531 passed, 6 skipped` without optional extras) | — |

---

## 4. Upgrade notes (v0.1.0 → v0.2)

1. **Block trust is now operator-configured on the server (`--trusted` / `CF_TRUST_BLOCKS`).**
   - *Before (v0.1.0):* callers could pass `"trusted": true` in `POST /fabric/blocks` JSON or
     MCP `memory_add` arguments to write `routable` blocks directly.
   - *Now (v0.2):* request-body `"trusted"` fields are ignored (`sidecar/blocks.py`,
     `sidecar/server.py`, `sidecar/mcp.py`). Blocks default to `lifecycle_status="draft"`
     (quarantined). To accept agent writes as `routable`, start the server with
     `cfx serve --trusted` (or `CF_TRUST_BLOCKS=1`) or `cfx mcp --trusted`, or ingest blocks
     via `cfx add --store <db> --file <json> --trusted`.
2. **CORS is now opt-in via an explicit origin allow-list (`--cors-origin` / `CF_CORS_ORIGINS`).**
   - *Before (v0.1.0):* `cfx serve` emitted permissive `Access-Control-Allow-Origin: *`
     headers on loopback.
   - *Now (v0.2):* CORS is off by default and any request with a browser `Origin` header is
     rejected `403` unless that origin is listed via `--cors-origin <origin>` (repeatable) or
     `CF_CORS_ORIGINS=http://localhost:3000,...`. Non-browser CLI/SDK tools (no `Origin`
     header) require no change.
3. **`cfx seal` reads the private signing seed from a file or env var, never `argv`.**
   - *Before (v0.1.0):* `cfx seal` accepted `--key <hex>` on the command line.
   - *Now (v0.2):* pass `--key-file <path>` (or set `CF_SEAL_KEY_FILE` / `CF_SEAL_KEY`)
     containing the 64-hex-char (32-byte) Ed25519 seed.
4. **Dev tools (`pytest`, `mypy`, `ruff`) live in `[dependency-groups] dev`, not `--extra dev`.**
   - *Before (v0.1.0):* `uv sync --extra dev` installed test/lint tooling.
   - *Now (v0.2):* `dev` is a PEP 735 dependency group installed by default on bare `uv sync`
     and `uv run`. Remove `--extra dev` from scripts and CI invocations (passing `--extra dev`
     now fails because no `dev` extra exists). Optional runtime extras remain
     `--extra embeddings`, `--extra langgraph`, and `--extra observability`.
5. **SQLite schema auto-migration.**
   - Existing `.db` stores from v0.1.0 open cleanly: `SqliteStore` migrates `compile_log` in
     place to add the `purpose` column and creates the `fold_log` and `fold_requests` tables
     automatically.

---

## 5. Clean-clone verification on Windows

Verified on 2026-10-20 by cloning `C:/dev/Context-Fabric` (`1b2227e`) into a fresh temporary
directory (`%TEMP%\cfx_clean_clone_verify_1560867470`) on Windows (CPython 3.13.12, `uv`) and
running every `README.md` Quickstart command in sequence:

| # | Command run in clean clone | Exit | Observed result |
| --: | :--- | --: | :--- |
| 1 | `uv sync` | `0` | Created `.venv`, built `context-fabric`, installed 32 packages (including default `dev` group: `pytest`, `mypy`, `ruff`). |
| 2 | `uv run python -m evals.spike_deterministic` | `0` | `spike_deterministic: ALL CHECKS PASS` (`content_hash = sha256:0821d0707c21983bd...`, prefix-stable `True`). |
| 3 | `uv run pytest -q` | `0` | `531 passed, 6 skipped in 103.97s` (`pytest --collect-only -q` collects `536` tests + 1 module-level skip without `--extra langgraph`). |
| 4 | `uv run cfx repro` | `0` | `cfx repro: OK - 99 offline checks passed`. |
| 5a | `uv run cfx serve --upstream http://127.0.0.1:8000 --fabric fabric.db` *(before `cfx add`)* | `2` | `cfx serve: fabric file not found: fabric.db` — README listed `cfx serve --fabric fabric.db` before `cfx add` created `fabric.db` (**fixed in `README.md`**). |
| 6a | `uv run cfx demo --scenario fleet --timeout 2` *(no upstream server)* | `2` | `cfx demo: request failed: timed out` (expected when no model server is listening on `:8000`; also logged a `ConnectionAbortedError` traceback on stderr — see §5.2). |
| 6b | `uv run cfx demo --scenario fleet --mock` | `0` | `broadcast 6 reqs 40203 prompt_tok acc 6/6` vs `compile 6 reqs 12832 prompt_tok acc 6/6`; receipt written. |
| 7a | `uv run cfx demo --repo . --timeout 2` *(no upstream server)* | `2` | `cfx demo: request failed: timed out` (expected without a live upstream). |
| 7b | `uv run cfx demo --repo . --mock` | `0` | `fixture: repo . (7 files, 9326 raw tokens)`, `raw 56058` vs `compile 4348` prompt tokens, cartridge `cfx_repo_8a08abd7` (**updated `REPRODUCE.md` from `9,325` / `cfx_repo_af83f34b`**). |
| 8a | `uv run cfx add --store fabric.db --file block.json --trusted` *(PowerShell `Set-Content -Encoding utf8`)* | `2` | `cfx add: cannot read block JSON: Unexpected UTF-8 BOM (decode using utf-8-sig)` — Windows PowerShell 5.1 `-Encoding utf8` writes a BOM that `encoding="utf-8"` rejects (see §5.2). |
| 8b | `uv run cfx add --store fabric.db --file block.json --trusted` *(UTF-8 without BOM / ASCII)* | `0` | `block_id: blk_03c9a271eb81c47a`, `lifecycle_status: routable`; created `fabric.db`. |
| 9 | `uv run cfx serve --upstream <local> --fabric fabric.db --obs-fold --obs-store obs.db --obs-n 1 --obs-k 1` + chat request | `0` | Returned `X-Fabric-Compiled: 1` and `X-Fabric-Packet-Id: cpk_f8d9a09fa4fee763`; recorded manifest in `fabric.db` and folded observation in `obs.db`. |
| 10 | `uv run cfx mcp --store fabric.db` *(JSON-RPC `initialize` + `tools/call memory_search` on stdin)* | `0` | Returned matching routable block `blk_03c9a271eb81c47a` (`score: 2`). |
| 11 | `uv run cfx replay --store fabric.db --packet-id cpk_f8d9a09fa4fee763` | `0` | `REPLAY OK cpk_f8d9a09fa4fee763 sha256:aa64d5e5...` |
| 12 | `uv run cfx seal --store fabric.db --key-file seal.key --out epochs.json` | `0` | `epoch_id: epc_ca48e3f5380b487b`, `packets: 1`. |
| 13 | `uv run cfx verify epochs.json --pubkey <hex>` | `0` | `OK: 1 epoch(s) verified, key_id=10ba682c8ad13513`. |
| 14 | `uv run cfx replay-fold --store obs.db` | `0` | `FOLD REPLAY OK 1 sha256:c62d5a67...` |
| 15 | `uv run cfx replay-fold --store obs.db --expand` | `0` | Emitted original unfolded request JSON after verifying `obs_b4ba8da8527d` content hash. |
| 16 | `uv run cfx expand --store obs.db ctx://obs/b4ba8da8527d` | `0` | Printed the exact 2-line folded tool output verbatim. |

Temporary clone directory removed after verification (`CLEANUP_OK=True`).

### 5.1 Documentation fixes applied in Track T4
- **`README.md` Quickstart**: placed `cfx add --store fabric.db ...` (with stdin/file example)
  *before* `cfx serve --fabric fabric.db` so `fabric.db` exists when `cfx serve` starts; added
  `--mock` notes on `cfx demo` commands for zero-GPU verification; updated test counts from
  `486` (`481 pass, 6 skip`) to `536` (`531 pass, 6 skip`).
- **`docs/engineering/NUMBERS.md`**: updated `test_count` to `536` and added `tests-486` and
  `486 tests` to `stale_patterns`.
- **`docs/engineering/REPRODUCE.md`**: updated `cfx demo --repo . --mock` expected token count
  and cartridge ID (`9,326 raw_tokens`, `cfx_repo_8a08abd7`) following the `1b2227e` path
  redaction in `evals/a4_concurrency.py`.
- **`docs/engineering/SIDECAR.md`**: added the concise *"Using `cfx serve` with an agent tool"*
  guide and updated the `cfx` subcommand list against `cfx --help`.
- **`docs/engineering/OBS-FOLD.md`**: corrected the environment variable list (`CF_OBS_FOLD`
  and `CF_OBS_STORE` are implemented; `--obs-n`, `--obs-k`, and `--obs-max-tokens` are CLI
  flags only).
- **`docs/engineering/MCP.md`**: added the shipped `expand` tool to the MCP tool list and
  schema table.

### 5.2 Code problems recorded for post-freeze cleanup (frozen during B8 pilot)
The following code issues were identified during clean-clone verification and CLI inspection
but live in frozen modules (`sidecar/cli.py`, `sidecar/server.py`, `sidecar/demo.py`,
`sidecar/replay.py`, `sidecar/mcp.py`) and must wait until the B8 pilot freeze ends:

1. **UTF-8 BOM rejection on Windows (`sidecar/cli.py:337`, `sidecar/replay.py:137`)**:
   `cfx add --file` and `cfx seal --key-file` read files with `encoding="utf-8"`. On Windows
   PowerShell 5.1, `Set-Content -Encoding utf8` prepends a UTF-8 BOM (`\ufeff`), causing
   `json.loads` / `bytes.fromhex` to fail with exit code 2. Switching file reads in
   `sidecar/cli.py` and `sidecar/replay.py` to `encoding="utf-8-sig"` will accept both BOM and
   BOM-less UTF-8 transparently.
2. **Client-disconnect `ConnectionAbortedError` in `_send_json` (`sidecar/server.py:281,819`)**:
   `do_POST` and `do_GET` catch `(BrokenPipeError, ConnectionResetError)`, or let
   `ConnectionAbortedError` (`[WinError 10053]` on Windows, a subclass of `ConnectionError`
   rather than `BrokenPipeError`/`ConnectionResetError`) escape when a client times out while
   `_proxy` is waiting on a refused upstream connection and then writes a `502` response.
   Catching `ConnectionError` in `do_POST`/`do_GET`/`do_OPTIONS` (or inside `_send_json`)
   suppresses the spurious `socketserver` traceback.
3. **Non-ASCII em-dash in CLI output (`sidecar/demo.py:362`, `sidecar/cli.py:158`)**:
   `cfx demo --mock` prints `MOCK MODE — canned responses` with an unescaped `\u2014` em-dash,
   and `cfx serve --help` has an em-dash in `--cors-origin` help text. On Windows consoles
   defaulting to `cp1252`/non-UTF-8 pipes, this renders as `\ufffd` or Mojibake (unlike
   `sidecar/repro.py`, which ASCII-escapes via `_safe()`).
4. **Missing env fallbacks for `--obs-n`, `--obs-k`, `--obs-max-tokens` (`sidecar/cli.py:74-92`)**:
   `OBS-FOLD.md` originally documented `CF_OBS_N`, `CF_OBS_K`, and `CF_OBS_MAX_TOKENS`, but
   `sidecar/cli.py` only wires `CF_OBS_FOLD` and `CF_OBS_STORE` from `os.environ`. Either add
   `_int_env("CF_OBS_N", ...)` / `_int_env("CF_OBS_K", ...)` / `_int_env("CF_OBS_MAX_TOKENS")`
   in `sidecar/cli.py` after the freeze or keep them CLI-only (docs now state CLI-only).
5. **CORS `Access-Control-Allow-Headers` omits `X-Fabric-Purpose` (`sidecar/server.py:63`)**:
   `_CORS_HEADERS` lists `Content-Type,Authorization,X-Fabric-Agent,X-Fabric-Role`, while
   `_compile` (`sidecar/server.py:495`) also reads `X-Fabric-Purpose`. Adding
   `X-Fabric-Purpose` to `_CORS_HEADERS` allows allow-listed browser clients to send purpose
   headers across CORS preflights.
6. **`SERVER_VERSION` constant in `sidecar/mcp.py:39`**: hardcoded to `"0.1.0"`; bump junto
   with `pyproject.toml` when tagging `v0.2.0`.

---

## 6. Release checklist (v0.2.0)

### Phase A — Pre-freeze release candidate (completed in Track T4)
- [x] Draft `docs/engineering/RELEASE-v0.2.md` with shipped scope, explicit non-claims,
      citable numbers from `NUMBERS.md`, upgrade notes, and clean-clone verification log.
- [x] Run clean-clone verification on Windows (`git clone C:/dev/Context-Fabric <tmp>`),
      execute every README Quickstart command, and fix doc discrepancies.
- [x] Document new sidecar agent-tool workflow (`--obs-fold`, `cfx expand`, `cfx replay-fold`,
      security flags) in `docs/engineering/SIDECAR.md`, verified against `cfx <cmd> --help`.
- [x] Update `CHANGELOG.md` `[Unreleased]` with all work merged since `v0.1.0`.
- [x] Pass all 5 mandatory quality gates with exit code 0:
  - [x] `uv run ruff check .`
  - [x] `uv run ruff format --check .`
  - [x] `uv run mypy protocol evals kernel sidecar adapters`
  - [x] `uv run python -m pytest -q`
  - [x] `uv run python scripts/check_docs_numbers.py`
- [x] Verify `pyproject.toml`, `uv.lock`, and all frozen code/B8 files are untouched (`git status`).

### Phase B — Post-B8 pilot cut (`v0.2.0` tag)
- [ ] Confirm the B8 v3 pilot has completed and its final result/receipts are committed and
      registered in `docs/engineering/NUMBERS.md` (if citable) and `README.md`.
- [ ] Apply optional post-freeze code fixes from §5.2 (`utf-8-sig` file reads,
      `ConnectionError` catch in `sidecar/server.py`, ASCII dashes in `demo.py`/`cli.py`,
      `X-Fabric-Purpose` in `_CORS_HEADERS`).
- [ ] Bump version from `0.1.0` to `0.2.0` in `pyproject.toml` and `sidecar/mcp.py`
      (`SERVER_VERSION`), run `uv lock`, and update the `README.md` status badge.
- [ ] Roll `CHANGELOG.md` `## [Unreleased]` to `## [0.2.0] - 2026-10-XX`.
- [ ] Re-run all 5 gates + `uv run cfx repro` (all must exit `0`).
- [ ] Commit, push, and create annotated tag `git tag -a v0.2.0 -m "Context Fabric v0.2.0"`.
