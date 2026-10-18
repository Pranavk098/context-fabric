# B4 Pre-Registration — divergent-role compiled context vs prefix-cache-only, with a quality gate

**Status: PRE-REGISTERED. Frozen 2026-10-18 20:44 UTC, before any model output or run on
this fixture.** The author (builder/experiment agent) has not run vLLM, the harness's
`--live` mode, or any model request against this fixture at the time of writing. Everything
below (fixture, roles, objectives, scorer, arms, measurement, criteria) is fixed now;
deviations are allowed only under the anti-tuning rule at the bottom. This document is the
binding specification for `evals/b4_divergent.py` and `tests/test_b4_divergent.py`.

## 1. Question

On a real repository at real context size, does **compiled per-role context** beat
**prefix-cache-only context** on a task with a *quality gate* (not a substring match)? This
is the divergent-role multi-agent case, which is Context Fabric's claimed white space:
same-history multi-agent is mostly what vLLM prefix caching already gives (~2× vs a shared
raw prefix). B4 must show — or falsify — that compiled per-role packets win where it
matters: quality with no regression, and *uncached* prefill tokens (the real KV/prefill
cost), not just billed prompt tokens.

This is an engineering falsification run on real bytes, not a benchmark claim. If the
compile arm loses on quality, that is a published kill signal, not a reason to re-tune.

## 2. Fixture (fixed now)

- Repo: `https://github.com/encode/httpx`, the same pinned commit as the pilot.
- Exact commit: `b5addb64f0161ff6bfe94c124ef76f6a1fba5254` (2026-02-23, "Adapt
  test_response_decode_text_using_autodetect for chardet 6.0 (#3773)").
- Bundle rule: an **explicit 4-file slice** (a sub-slice of the pilot's 10-file bundle,
  sized so every raw arm fits `--max-model-len 8192`). Files, in this fixed order:
  1. `httpx/__init__.py`
  2. `httpx/__version__.py`
  3. `httpx/_api.py`
  4. `httpx/_auth.py`
- Rendering (deterministic, the only transformation): each file's raw UTF-8 bytes
  concatenated in the order above, one blank line between files, each file prefixed by a
  header line `===== FILE: <relative path> =====`. No other edits. The exact rendered bundle
  is what the raw arms send (as the shared system content) and what the token count refers
  to. Measured pre-run (tiktoken `o200k_base`, the same tokenizer `kernel.cartridge.serializer`
  uses): **6,214 tokens**, 26,990 characters. The run-time dry-run value is authoritative and
  recorded verbatim in the artifact.
- Slice sizing (declared up front): the largest raw-arm prompt (arm B's planner: arm tag +
  bundle + role instruction + objective) measures **6,277 tokens**, comfortably under the
  8,192-token window the WSL runner uses. No raw arm may exceed 8,192; the dry-run asserts
  this and aborts the run if it is violated.

## 3. Roles, objectives, and the quality gate (fixed now)

Four agents with genuinely different role slices (each needs a different file/fact). The
scorer is **stronger than a substring match**: an answer passes only if it contains **all
required facts** (case-insensitive substring, conjunctive) **and none of the forbidden wrong
facts** (case-insensitive substring). Forbidden facts are specific plausible-but-wrong
answers a confabulating small model would produce; they are chosen so a correct terse answer
never contains them and so no compiled packet ever carries them (verified at build time).

| role | objective (sent as the user message) | required (all) | forbidden (none) | source file (real code) |
|---|---|---|---|---|
| planner | Which module does `httpx/__init__.py` re-export that provides the synchronous `Client` and asynchronous `AsyncClient` classes? Give the `_X` module name only. | `_client` | `_models` | `httpx/__init__.py` (`from ._client import *`) |
| implementer | In httpx, which function do the top-level helpers `get()` and `post()` delegate to? Give the function name only. | `request` | `stream` | `httpx/_api.py` (`get()`/`post()` both `return request(...)`) |
| tester | What is the exact value of `__version__` in `httpx/__version__.py`? | `0.28.1` | `0.27`, `0.28.0`, `0.29` | `httpx/__version__.py` (`__version__ = "0.28.1"`) |
| reviewer | Which auth class in `httpx/_auth.py` base64-encodes `username:password` and prefixes it with `Basic `? Give the class name only. | `basicauth` | `digestauth`, `netrcauth`, `functionauth` | `httpx/_auth.py` (`class BasicAuth`, `_build_auth_header` returns `f"Basic {token}"`) |

The scorer function is identical for every arm:

```
score_quality(text, spec) -> (passed, missing_required, present_forbidden)
passed = (missing_required is empty) AND (present_forbidden is empty)
```

Build-time construction checks (in `evals/b4_divergent.py`, abort the run before any request
if any fails):
1. every required fact is present (case-insensitive) in the rendered raw bundle — so arms A
   and B can answer every role;
2. every required fact is present in its role's fact block **and** in that role's compiled
   packet — so arm C can answer every role;
3. no forbidden fact appears in any fact block **nor** in any compiled packet — so arm C is
   never primed to emit a forbidden wrong fact.

Known limitation, stated up front (mirroring the pilot's honesty): required/forbidden are
substring facts, so a verbose correct answer that happens to mention a forbidden distractor
fails, and a wrong answer that happens to include every required fact without any forbidden
one passes. Objectives instruct "give the … only" and `max_tokens` is small, minimizing but
not eliminating this. The asymmetry is symmetric: the scorer is identical for all arms. The
raw bundle *does* contain the forbidden distractors (e.g. `_models`, `stream`, `DigestAuth`
are real and present in the raw bytes); the compiled packets *do not*. That raw-vs-clean
contrast is the point under test, not a defect.

## 4. Fabric (fixed now)

One hand-derived fact block per role, grounded in the real source, written through the same
`ContextBlock` shape the compiler consumes (`trust_domain="default"`,
`lifecycle_status=routable`, `memory_tier=ledger`, `fidelity=extract`, `source_event_seq`
1..4 ascending). Divergence is enforced twice: each block is `denied_roles` to the three
roles that must not see it (hard policy), and each block's content is the only one whose
tokens overlap its role's objective (BM25 recall). Both gates are deterministic and asserted
by the parity/divergence tests.

| block_id | denied_roles | carries required fact for | content (grounded in) |
|---|---|---|---|
| `blk_init` | implementer, tester, reviewer | planner | `httpx/__init__.py` re-exports the public API; `Client`/`AsyncClient` come from `._client` |
| `blk_api` | planner, tester, reviewer | implementer | `httpx/_api.py` verb helpers `get()`/`post()` delegate to `request()` |
| `blk_version` | planner, implementer, reviewer | tester | `httpx/__version__.py` sets `__version__ = '0.28.1'` |
| `blk_auth` | planner, implementer, tester | reviewer | `httpx/_auth.py` `BasicAuth._build_auth_header` base64-encodes and prefixes `Basic ` |

None of the four blocks mentions any forbidden fact (construction check 3). Blocks are
compiled with the shipped `compile_packet` via the LangGraph adapter's `compile_node_packet`
path, so arm C's packets are byte-identical to what a LangGraph workflow would produce
(asserted in `tests/test_langgraph_parity.py` for the adapter generally, and in
`tests/test_b4_divergent.py` for this fixture).

## 5. Arms (fixed now)

All three arms express the same 4-agent workflow; only how each agent's model input is built
differs. Every arm runs **all 4 roles × 3 repeats = 12 requests** in a fixed order
(planner, implementer, tester, reviewer) with the objective as the user message.

- **A — broadcast**: every agent gets the **full raw bundle** as its system message (byte-
  identical across all 12 requests; no role identity encoded, matching the demo's broadcast
  baseline).
- **B — prefix-cache-only**: every agent gets the **shared raw bundle + a role instruction
  appended after it** — identical leading bytes for all agents, role instruction as the tail.
  This is the strongest naive baseline: vLLM prefix caching gets its best case (one shared
  prefix, tiny per-role tails). This arm is the comparison arm for the criteria.
- **C — compile**: each agent gets its **per-role compiled packet** (via
  `compile_node_packet` + `node_model_input`, the LangGraph adapter). Packets differ by role
  (denied_roles + objective-driven recall), so there is no shared prefix.

**Cache isolation (critical):** vLLM's prefix cache persists across requests and runs, so
each arm's system content is prefixed with a **unique leading arm tag**
(`B4::broadcast::\n`, `B4::prefix::\n`, `B4::compile::\n`) that is byte-identical within an
arm but distinct between arms. This prevents arm A's raw bundle from warming arm B's cache
(which would otherwise make B's uncached-token count artificially tiny and unfair to C) and
vice-versa. The tag is inside the system message and counted in the token totals. Within
arm B, agents share `tag + raw bundle` as the leading prefix and diverge only on the role
instruction; within arm A they share the whole system message; arm C has no sharing.

Order: **A, then B, then C**, in one vLLM session, tags isolating the cache. Each arm's
first request is a cold prefill (intended — that is the honest uncached-token cost); later
requests within an arm hit whatever prefix is shared. No smoke pass; the WSL runner's
engine-warmup request (tiny, unrelated prefix) precedes the harness.

## 6. Measurement (fixed now)

Per request: prompt tokens (billed), completion tokens, latency (OpenAI-compatible
response), and the response text. Per arm: request count, prompt/completion token totals,
wall time, mean latency, pass count, and quality (passes / 12). Upstream: vLLM Prometheus
`/metrics` parsed tolerantly — `vllm:prefix_cache_hits_total` and
`vllm:prefix_cache_queries_total` deltas per arm (the hits counter is in **tokens**, so
`uncached_tokens = prompt_tokens_total − prefix_cache_hits_delta`), and peak KV-cache usage
via a ~50 ms sampler — the same helpers the demo/pilot use; `null` when unavailable. Model:
`Qwen/Qwen2.5-1.5B-Instruct` on the local RTX 5070 8 GB, `temperature=0.0`, non-streaming,
`max_tokens=48`. Price envs unset; no cost figures.

## 7. Success criteria (fixed now)

Baseline arm = **B** (prefix-cache-only). `n = 4 roles × 3 repeats = 12 requests per arm`.

- **PASS** iff **both** hold:
  1. **No quality regression:** `C.passes >= B.passes − 1` (tolerance = 1 request out of 12;
     compiled quality is within one request of the prefix-cache-only baseline).
  2. **Compiled wins on uncached tokens:** `C.uncached_tokens <= B.uncached_tokens`
     (factor 1.0× — compiled's no-sharing prefill must not exceed the prefix-cache-only
     baseline's cold first-request prefill plus its tails).
- **KILL** iff `C.passes < B.passes − 1` (a real quality regression beyond tolerance). This
  is reported as the kill signal regardless of any token advantage — a smaller context that
  is *worse* is not a win.
- **NO_TOKEN_WIN** iff quality holds (`C.passes >= B.passes − 1`) but
  `C.uncached_tokens > B.uncached_tokens` — compiled context keeps quality but does **not**
  beat prefix-cache-only on uncached prefill, a negative result on the token dimension
  (reported as such, not a PASS).
- **INCONCLUSIVE** if the `/metrics` counters are unavailable (null) → the token criterion is
  unevaluable and only quality is reported; or if an arm has > 25% error requests (4+ of 12)
  → that arm's numbers are flagged unreliable and the comparison is inconclusive.

Ties and errors: a request that errors (HTTP failure) counts as a failed request
(`passes=0`) and is disclosed verbatim in the artifact; every role and every repeat is
reported, pass or fail. No cherry-picking.

Verdicts are reported plainly as PASS / KILL / NO_TOKEN_WIN / INCONCLUSIVE with the numbers.
If compiled loses on quality, that is a published kill signal, not a reason to re-tune the
fixture.

## 8. Anti-tuning rule

**One iteration is allowed ONLY for tooling/environment failures** — a crash, a timeout, an
OOM, a vLLM launch/context-length rejection, or a wiring bug in `evals/b4_divergent.py` that
prevents measurement. Such an iteration is disclosed exactly (what changed and why) in a
follow-up note. **Never** after seeing results: roles, objectives, required/forbidden facts,
fact-block content, bundle file list, arm construction, or success criteria are frozen as of
this document. If the compile arm fails a criterion, it is reported as failed; a new
experiment would need a new, separately dated pre-registration.

## 9. Run environment (fixed now)

WSL2 Ubuntu, the established `~/.venvs/vllm`, run through `scripts/wsl_run_eval.sh` (its
`REPO` is overridable via `CF_REPO` so the eval runs from this worktree; the default is
unchanged). The runner serves `Qwen/Qwen2.5-1.5B-Instruct --enable-prefix-caching
--max-model-len 8192 --enforce-eager --gpu-memory-utilization 0.85`. GPU: RTX 5070 Laptop
8 GB (single GPU). The 4-file slice is sized so every raw arm fits the 8,192 window (largest
measured 6,277 tokens; dry-run asserts `< 8192`).

## 10. Artifacts (to be produced)

- `evals/reports/b4_divergent_<ts>.json` — full receipt (records, arms, cache, criteria
  verdicts, notes) for both `--dry-run` and `--live` runs (the receipt's `kind` distinguishes
  them; a live receipt is the only source of any PASS/KILL verdict).
- `tests/test_b4_divergent.py` — offline tests (no model, no network).

Pre-stated caveats: single GPU; one 1.5B model; n = 4 roles × 3 repeats = 12 requests/arm;
four short fact-style objectives (information-presence scorer with required+forbidden facts,
not a judge and not an executable test — declared in §3); warm-cache caveats apply only
within an arm (tags isolate arms); the first request of each arm is cold prefill.
