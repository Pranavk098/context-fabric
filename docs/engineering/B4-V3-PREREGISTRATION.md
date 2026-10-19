# B4 v3 Pre-Registration — the repo does not fit the window

**Status: PRE-REGISTERED. Frozen 2026-10-19 03:20 UTC, before any question was run through a
model.** The only thing run before this freeze is the **feasibility check** (§3): each
candidate model was started in vLLM at `--max-model-len 16384` and probed with `/v1/models`
plus one 1-token warm-up request at `temperature=0.0` — no objective text, no scorer, no
evaluation. Everything below (fixture, arms, scorer, models, criteria) is fixed now;
deviations are allowed only under the anti-tuning rule (§8). This document is the binding
specification for `evals/b4_v3.py`, `tests/test_b4_v3.py`, and the exact-import variant of
`evals/mech_fabric.build_blocks` (the `import_statements=True` flag).

### Why v3 exists

B4 v2 (`B4-RESULT.md`) falsified "compiled per-role context beats prefix caching" on a
**fits-in-context** workload: a ~8k-token 4-file slice where every arm fit
`--max-model-len 8192`. B4 v3 changes exactly the thing v2 could not test: the **repository
no longer fits the window**. The full `httpx/` package (~64k tokens) is the fixture and the
window is `--max-model-len 16384`, so the raw/truncate baseline cannot carry the whole
repository. The three arms are truncate vs retrieval vs compile, each measured against the
same frozen objectives and the same frozen scorer.

## 1. Question

On a real repository that **does not fit** the context window, does the **compile** arm
(mechanical fabric over the full package, with a shared Tier-0 cartridge + terse answer
contract + exact import statements, budget 4,000 tokens, via the LangGraph adapter) beat a
**retrieval** baseline (AST chunks, BM25 over the objective, top chunks to 4,000 tokens) and
a **truncation** baseline (leading bytes of the full package that fit 16,384 tokens)? The
success bar is deliberately steep: compile must be at least as good as retrieval **and**
within tolerance of truncation **and** bill a third of the tokens truncation bills. This is
an engineering falsification run on real bytes, not a benchmark claim.

## 2. Fixture (fixed now)

- Repo: `https://github.com/encode/httpx`, the pinned pilot commit
  `b5addb64f0161ff6bfe94c124ef76f6a1fba5254` (2026-02-23).
- Bundle rule: the **full package** — every `httpx/**/*.py` file in sorted relative-path
  order (23 files). It does **not** fit `--max-model-len 16384`: the rendered bundle
  measures **~64,778 tokens** (tiktoken `o200k_base`; the Qwen tokenizer count is the
  authoritative one, recorded at run time).
- Rendering (deterministic, the only transformation): each file's raw UTF-8 bytes in sorted
  order, one blank line between files, each file prefixed by `===== FILE: <path> =====`.
- The 20 objectives are blind and frozen in `evals/fixtures/b4v3_objectives.json` (sha256
  `5b2c1172…` per `docs/engineering/B4-V3-OBJECTIVES.md`; the harness re-verifies it,
  CRLF-normalized, at start and aborts on mismatch).

## 3. Models and feasibility (fixed now)

Two models, one GPU (RTX 5070 Laptop, 8 GiB), `temperature=0.0`, non-streaming,
`max_tokens=96`, served with vLLM `--max-model-len 16384 --enable-prefix-caching
--enforce-eager --gpu-memory-utilization 0.85`.

| model | feasibility at 16384 | used |
|---|---|---|
| `Qwen/Qwen2.5-1.5B-Instruct` | **serves OK** (`/v1/models` + 1-token warm-up, HTTP 200) | model 1 |
| `Qwen/Qwen2.5-3B-Instruct` | **cannot serve** — vLLM: "To serve at least one request with the model's max seq len (16384), 0.56 GiB KV cache is needed, larger than available 0.45 GiB"; estimated max length 13104 | rejected |
| `Qwen/Qwen2.5-0.5B-Instruct` | **serves OK** (`/v1/models` + 1-token warm-up, HTTP 200) | model 2 |

The 3B model cannot hold a 16,384-token KV cache alongside its 5.79 GiB of weights on 8 GiB.
Per the frozen design, the **second model is therefore `Qwen/Qwen2.5-0.5B-Instruct`**,
decided before freeze. Both final models are cached in the WSL HF cache.

## 4. Arms (fixed now)

All three arms express the same 20-objective workflow (5 each for planner, implementer,
tester, reviewer), **1 repeat**, in the fixed objective order, with the objective as the user
message. Every arm's system content is prefixed with a **unique leading cache tag**
(`B4v3::truncate::`, `B4v3::retrieval::`, `B4v3::compile::`) so the persistent vLLM prefix
cache cannot carry between arms. Arm order: **truncate, retrieval, compile**, one vLLM
session per model.

- **T — truncate**: render the full package (sorted paths, FILE headers), then keep the
  leading bytes that fit `16384 − objective − role line − max_tokens (96) − a 16-token
  chat-template margin`. Counted with the **Qwen tokenizer** (via `transformers`, not
  tiktoken). System = `tag + role line + leading prefix`.
- **R — retrieval baseline**: AST chunks — one per top-level function/class (full source
  segment) plus one per module header (module docstring + import statements). Okapi BM25
  (`kernel.router.recall.bm25_scores`) over the objective ranks the chunks; take the
  top-scoring chunks (score > 0, longest-greedy) until **4,000 Qwen tokens**. No role line.
- **C — compile**: the mechanical fabric over the full package (top-level signatures + first
  docstring lines + assignments + **exact `from … import …` statements**), with the
  cartridge pinned as a **shared Tier-0** (L1) and a **terse answer contract** (L3:
  "Reply with ONLY the comma-separated answer values…"), budget **4,000**, via
  `compile_node_packet` + `node_model_input` (the LangGraph adapter path). Role divergence
  comes **only** from a fixed file-ownership map; blocks from an owned file are denied to the
  three non-owning roles, blocks from unowned files and the project-wide cartridge are shared.

**File-ownership map (fixed now, role → files it owns):**

| role | owns files |
|---|---|
| planner | `_client.py`, `_config.py`, `_status_codes.py`, `_transports/base.py`, `_transports/default.py`, `_urls.py` |
| implementer | `_decoders.py`, `_models.py`, `_urls.py` |
| tester | `_api.py`, `_exceptions.py`, `_models.py`, `_status_codes.py` |
| reviewer | `_auth.py`, `_config.py`, `_content.py`, `_exceptions.py`, `_main.py`, `_models.py`, `_multipart.py` |

(All paths relative to the package root, i.e. `httpx/_client.py` etc. Files not listed — the
`__init__`/`__version__`/`_types`/`_urlparse`/`_utils`/`_transports` scaffolding — are shared
across roles.) Whether a role's required facts survive into its compiled packet is an
**outcome**, reported by the dry-run recall diagnostic, never a precondition.

## 5. Scorer (fixed now)

Case-insensitive **token-level** match. A token is a maximal run of `[A-Za-z0-9_.]` plus `-`
(kept inside tokens like `MD5-SESS`) and `/` (kept in mime types like
`application/octet-stream`). Required items are matched **longest-first** and **each match
consumes its tokens**; a forbidden item whose tokens already appear in the objective text is
**ignored** for that objective. Pass ⇔ every required item is matched and no forbidden item
is matched. This is the scorer change v2's explorer flagged (token-level, so `raw_host` ≠
`host`, `MD5` ≠ `MD5-SESS`, `aread` ≠ `read`, `RequestError` ≠ `request`, and a
restated-question answer is not tripped by its own distractor) — pre-registered here, not
retro-applied to v2.

## 6. Measurement (fixed now)

Per request: prompt tokens (billed), completion tokens, latency, response text, and the
per-objective pass/fail. Per arm: request count, prompt/completion token totals, wall time,
mean latency, pass count, quality, and error count. Upstream vLLM Prometheus `/metrics` is
parsed tolerantly for prefix-cache hits/queries deltas and peak KV usage (same helpers as the
pilot/demo; `null` when unavailable). `uncached_tokens = billed prompt tokens −
prefix-cache-hits delta`. **Uncached tokens, peak KV, and latency are reported but are not
criteria.** Token counts use the Qwen tokenizer; the offline dry-run falls back to tiktoken
and flags it when `transformers` is unavailable.

## 7. Success criteria (fixed now)

Per model. `n = 20 objectives × 1 repeat = 20 requests per arm`. A request that errors counts
as a fail; any arm with > 25% error requests (> 5 of 20) makes the run **INCONCLUSIVE**.

- **PASS** iff **all three** hold: `C.passes >= R.passes`, `C.passes >= T.passes − 2`, and
  `C billed prompt tokens <= T billed / 3`.
- **KILL** iff `C.passes < R.passes − 2` (a real quality regression beyond tolerance).
- **MIXED** otherwise.

## 8. Anti-tuning rule

**One iteration is allowed ONLY for tooling/environment failures** (a crash, an OOM, a vLLM
context-length rejection, or a wiring bug in `evals/b4_v3.py` / `evals/mech_fabric.py`).
Such an iteration is disclosed in a follow-up note. **Never** after seeing results: the
fixture, arms, scorer, ownership map, models, budget, `max_tokens`, or criteria are frozen as
of this document. If the compile arm fails a criterion, it is reported as failed; a new
experiment would need a new, separately dated pre-registration.

## 9. Run environment (fixed now)

WSL2 Ubuntu, `~/.venvs/vllm`, through `scripts/wsl_run_eval.sh` (`VLLM_MAX_LEN=16384`,
`CF_REPO` pointing at this worktree). Live command per model:

```sh
MSYS_NO_PATHCONV=1 wsl -d Ubuntu -- bash -c "VLLM_MAX_LEN=16384 VLLM_MODEL=Qwen/Qwen2.5-1.5B-Instruct CF_REPO=/mnt/c/dev/Context-Fabric CF_B4_FIXTURE=/mnt/c/Users/<user>/AppData/Local/Temp/opencode/httpx_pilot bash /mnt/c/dev/Context-Fabric/scripts/wsl_run_eval.sh evals.b4_v3"
```

(the second run replaces `VLLM_MODEL=Qwen/Qwen2.5-1.5B-Instruct` with
`VLLM_MODEL=Qwen/Qwen2.5-0.5B-Instruct`). The runner's engine warm-up request (tiny,
unrelated prefix) precedes the harness.

## 10. Artifacts (to be produced)

- `evals/reports/b4_v3_<ts>.json` — full receipt (records, arms, cache, criteria verdicts,
  notes) for `--dry-run` and live runs; the live receipt is the only source of a
  PASS/KILL/MIXED verdict.
- `tests/test_b4_v3.py` — offline tests (scorer cases, objectives sha256 guard, exact-import
  fabric + ownership divergence, AST-chunk retrieval baseline, arms built correctly, dry-run
  determinism + recall).

Pre-stated caveats: single GPU; two small Qwen models (1.5B and 0.5B); n = 20 objectives × 1
repeat per arm; short identifier/value-style objectives scored by an information-presence
scorer (required+forbidden token match, not a judge and not an executable test). The
truncation baseline can only see the leading ~16k tokens of the sorted package, so facts in
later files (`_status_codes.py`, `_transports/*`, `_types.py`, `_urls.py`, `_utils.py`) are
structurally out of its reach; the retrieval baseline chunks only functions/classes and
module headers, so module-level constants are structurally out of its reach. Those limits are
the point: the experiment measures how compile does when the honest baselines are both
handicapped by the window.
