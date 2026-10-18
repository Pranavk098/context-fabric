# B4 Pre-Registration v2 — mechanical fabric, divergent-role compiled context vs prefix-cache-only

**Status: PRE-REGISTERED. Frozen 2026-10-18 21:28 UTC, before any model output or run on
this fixture.** The author (builder/experiment agent) has not run vLLM, the harness's live
mode, or any model request against this fixture at the time of writing. This document
**supersedes `B4-PREREGISTRATION.md` (v1)** before any run; v1 is kept, marked SUPERSEDED,
for the audit trail only. Everything below (fixture, mechanical fabric, ownership map,
objectives, scorer, arms, measurement, criteria) is fixed now; deviations are allowed only
under the anti-tuning rule at the bottom. This document is the binding specification for
`evals/b4_divergent.py`, `evals/mech_fabric.py`, and `tests/test_b4_divergent.py`.

### Why v2 exists (the v1 failure, stated once)

v1 passed "by construction": it hand-wrote one fact block per question (each block literally
contained its answer, so construction check 2 guaranteed the compile arm could answer), and
construction check 3 guaranteed the compile arm never saw the forbidden distractors that the
raw arms did. That tilted the comparison toward the compile arm before a single token was
generated — the exact credibility failure Order B exists to fix. v2 removes both tilts:

- the fabric is built **mechanically** from the source files by a reusable, deterministic
  ingester (`evals/mech_fabric.build_blocks`) that never reads objectives or answer keys (no
  per-question blocks);
- whether a required fact survives into a compiled packet is an **outcome to report**
  (per-objective "fact recall in packet"), never a precondition;
- forbidden facts are chosen by reading the **source**, not by inspecting packets.

## 1. Question

On a real repository at real context size, does **compiled per-role context** (built
mechanically from source symbols/assignments, no answer-carrying hand blocks) beat
**prefix-cache-only context** on a *quality-gated* task? The divergent-role multi-agent case
is Context Fabric's claimed white space: same-history multi-agent is mostly what vLLM prefix
caching already gives (~2× vs a shared raw prefix). B4 must show — or falsify — that
mechanically-compiled per-role packets win where it matters: quality with no regression, and
*uncached* prefill tokens, not just billed prompt tokens.

This is an engineering falsification run on real bytes, not a benchmark claim. If the compile
arm loses on quality, that is a published kill signal, not a reason to re-tune.

## 2. Fixture (fixed now)

- Repo: `https://github.com/encode/httpx`, the pinned pilot commit
  `b5addb64f0161ff6bfe94c124ef76f6a1fba5254` (2026-02-23).
- Bundle rule: an **explicit 4-file slice**, one substantive file per role, sized so every
  raw arm fits `--max-model-len 8192`. Files, in this fixed order:
  1. `httpx/_api.py`
  2. `httpx/_auth.py`
  3. `httpx/_status_codes.py`
  4. `httpx/_types.py`
- Rendering (deterministic, the only transformation): each file's raw UTF-8 bytes
  concatenated in the order above, one blank line between files, each file prefixed by a
  header line `===== FILE: <relative path> =====`. No other edits. The rendered bundle is the
  raw arms' shared system content and the token source of truth. Measured pre-run (tiktoken
  `o200k_base`, the tokenizer `kernel.cartridge.serializer` uses): **7,902 tokens**. The
  run-time dry-run value is authoritative and recorded verbatim.
- Slice sizing (declared up front): the largest raw-arm prompt (arm B's implementer objective:
  tag + bundle + role instruction + objective) measures **7,970 tokens**, under the 8,192-token
  window the WSL runner uses. No raw arm may exceed 8,192; the dry-run asserts this and aborts
  if violated.

## 3. Mechanical fabric (fixed now)

The fabric is **not hand-written**. `evals/mech_fabric.build_blocks(repo_root, files)` reads
only the 4 files (plus the shared `ast`/`extract_source`/`build_cartridge` helpers) and emits,
deterministically (files in listed order, then source order):

- **symbols** — one `fact` block per top-level class/function: its signature (from
  `kernel.cartridge.ast_extractor.extract_source`) plus its first docstring line
  (`ast.get_docstring`);
- **assignments** — one `fact` block per module-level and class-level `name = value`
  assignment (`ast.unparse`);
- **cartridge** — one project-wide `artifact` block carrying the `.cfx` hologram
  (`build_cartridge`, `budget_tokens=600`), `ref_uri=None`.

Import statements are **deliberately not** a block kind, so objectives that ask "which module
is X imported from" have a genuine recall gap (an honest outcome, not a hidden answer). The
ingester **never reads objectives or answer keys** — its only inputs are `(repo_root, files)`,
and a test (`tests/test_b4_divergent.py`) proves this: it feeds the ingester a `src.py` and a
separate `objectives.py`, and asserts no objective/key text from the latter enters the blocks.

**Role divergence — a fixed file-ownership map, not per-answer.** Each role owns exactly one
file; `evals/b4_divergent.build_mech_blocks` sets `denied_roles` mechanically so a block is
denied to every role that does not own its `ref_uri` file (the cartridge block is shared).
No block is authored for a specific question.

| role | owns file |
|---|---|
| planner | `httpx/_api.py` |
| implementer | `httpx/_auth.py` |
| tester | `httpx/_status_codes.py` |
| reviewer | `httpx/_types.py` |

## 4. Objectives and the quality gate (fixed now)

Twelve objectives, three per role, written **from reading the source**; every required fact is
answerable from the raw bundle (the only construction check, below). Nine of twelve need two
distinct facts (the "harder" half). The scorer is **stronger than a single substring**: an
answer passes iff it contains **all required facts** (case-insensitive substring, conjunctive)
**and none of the forbidden facts** (case-insensitive substring). Forbidden facts are plausible
wrong answers chosen from the source (a real symbol/value that is wrong here); none is chosen
by looking at a compiled packet.

| id | role | objective (user message) | required (all) | forbidden (none) |
|---|---|---|---|---|
| planner-1 | planner | Which two `_X` modules does `httpx/_api.py` import its `Client` and `Response` classes from? Answer `_X, _Y`. | `_client`, `_models` | `_urls` |
| planner-2 | planner | Which two `_X` modules does `httpx/_api.py` import `DEFAULT_TIMEOUT_CONFIG` and `URL` from? Answer `_X, _Y`. | `_config`, `_urls` | `_types` |
| planner-3 | planner | Which two functions in `_api.py` are the generic engine that every verb helper delegates to, and the streaming context-manager alternative? Answer `name, name`. | `request`, `stream` | `delete` |
| implementer-1 | implementer | Which two class-level flags does the `Auth` base class in `_auth.py` declare, so subclasses can signal that the request or response body must be read? Answer `flag, flag`. | `requires_request_body`, `requires_response_body` | `auth_flow` |
| implementer-2 | implementer | Which two auth classes in `_auth.py` adapt a plain callable and a netrc file, respectively? Answer `class, class`. | `functionauth`, `netrcauth` | `digestauth` |
| implementer-3 | implementer | Which auth class in `_auth.py` parses a `WWW-Authenticate: Digest` challenge and supports the `MD5`, `SHA`, `SHA-256` and `SHA-512` algorithms? | `digestauth` | `basicauth` |
| tester-1 | tester | In `_status_codes.py`, give the two integer status codes for `IM_A_TEAPOT` and `TOO_MANY_REQUESTS`. Answer `int, int`. | `418`, `429` | `451` |
| tester-2 | tester | In `_status_codes.py`, give the reason phrases for status codes 404 and 503. Answer `phrase, phrase`. | `not found`, `service unavailable` | `bad gateway` |
| tester-3 | tester | Which `codes` classmethod in `_status_codes.py` returns `True` for both 4xx and 5xx status codes? | `is_error` | `is_client_error` |
| reviewer-1 | reviewer | Which two type aliases in `_types.py` name the accepted types for a request URL and for request headers? Answer `alias, alias`. | `urltypes`, `headertypes` | `cookietypes` |
| reviewer-2 | reviewer | Which two type aliases in `_types.py` are the request and response body types, both `Union[str, bytes, Iterable[bytes], AsyncIterable[bytes]]`? Answer `alias, alias`. | `requestcontent`, `responsecontent` | `requestdata` |
| reviewer-3 | reviewer | Which class in `_types.py` is the synchronous byte-stream base, defining `__iter__` and `close`? | `syncbytestream` | `asyncbytestream` |

The scorer function is identical for every arm.

**The one construction check** (`evals/b4_divergent.verify_raw_answerable`, aborts before any
request): every required fact is present (case-insensitive) in the rendered raw bundle — so
every objective is answerable from the raw context. There is **no** check that a compiled
packet contains any required fact: that is measured and reported as an outcome (dry-run recall
table, §6).

Known limitation, stated up front (mirroring the pilot's honesty): required/forbidden are
substring facts, so a verbose correct answer that mentions a forbidden distractor fails, and a
wrong answer that happens to contain every required fact without any forbidden one passes;
substring recall can false-positive (e.g. `_get_client_nonce` contains `_client`). These
asymmetries apply equally to every arm.

## 5. Arms (fixed now)

All three arms express the same 12-objective workflow; only how each agent's model input is
built differs. Every arm runs **all 12 objectives × 3 repeats = 36 requests** in fixed order
(planner-1..3, implementer-1..3, tester-1..3, reviewer-1..3), with the objective as the user
message.

- **A — broadcast**: every request gets the **full raw bundle** as its system message
  (byte-identical across all 36 requests; no role identity).
- **B — prefix-cache-only**: every request gets the **shared raw bundle + a role instruction
  appended** — identical leading bytes, per-role tail. The strongest naive baseline: vLLM
  prefix caching's best case. This arm is the criterion baseline.
- **C — compile**: each request gets its role's **mechanically-compiled packet** (via
  `compile_node_packet` + `node_model_input`; the cartridge hologram enters as an `artifact`
  block through the fabric, not as a tier-0 pin). Packets differ by role only through the
  ownership map.

**Cache isolation (critical):** vLLM's prefix cache persists across requests and runs, so each
arm's system content is prefixed with a **unique leading arm tag** (`B4::broadcast::\n`,
`B4::prefix::\n`, `B4::compile::\n`), identical within an arm, distinct between arms. This
prevents arm A's raw bundle from warming arm B's cache (which would make B's uncached-token
count artificially tiny) and vice-versa. The tag is inside the system message and counted.
Order: **A, then B, then C**, one vLLM session. Each arm's first request is a cold prefill
(intended — the honest uncached-token cost). No smoke pass; the WSL runner's engine-warmup
request (tiny, unrelated prefix) precedes the harness.

## 6. Measurement (fixed now)

Per request: prompt tokens (billed), completion tokens, latency, response text, and the
per-objective pass/fail. Per arm: request count, prompt/completion token totals, wall time,
mean latency, pass count, quality (passes / 36), and per-objective pass rates. Upstream: vLLM
Prometheus `/metrics` parsed tolerantly — `vllm:prefix_cache_hits_total` /
`vllm:prefix_cache_queries_total` deltas per arm (hits counter is in **tokens**, so
`uncached_tokens = prompt_tokens_total − prefix_cache_hits_delta`), and peak KV-cache usage via
a ~50 ms sampler — the same helpers the demo/pilot use; `null` when unavailable. Model:
`Qwen/Qwen2.5-1.5B-Instruct`, RTX 5070 8 GB, `temperature=0.0`, non-streaming, `max_tokens=48`.
Price envs unset.

**Recall diagnostic (dry-run only, allowed because it uses no model):** after building arm C
offline, the dry-run prints, per objective, whether each required fact is present
(case-insensitive) in that role's compiled packet ("fact recall in packet"). This is a
diagnostic of the mechanical fabric, never a precondition; the live run applies the frozen
criteria regardless of what the dry-run recall showed.

## 7. Success criteria (fixed now)

Baseline arm = **B** (prefix-cache-only). `n = 12 objectives × 3 repeats = 36 requests per
arm`. Tolerance = `ceil(0.1·n) = ceil(3.6) = 4` requests.

- **PASS** iff **both** hold:
  1. **No quality regression:** `C.passes >= B.passes − 4`.
  2. **Compiled wins on uncached tokens:** `C.uncached_tokens <= B.uncached_tokens`
     (factor 1.0× — compiled's no-sharing prefill must not exceed the prefix-cache-only
     baseline's cold first-request prefill plus its tails).
- **KILL** iff `C.passes < B.passes − 4` (a real quality regression beyond tolerance),
  reported regardless of any token advantage — a smaller context that is *worse* is not a win.
- **NO_TOKEN_WIN** iff `C.passes >= B.passes − 4` but `C.uncached_tokens > B.uncached_tokens`
  (quality held but compiled did not beat prefix-cache-only on uncached prefill).
- **INCONCLUSIVE** if `/metrics` counters are unavailable (token criterion unevaluable) or an
  arm has > 25% error requests (10+ of 36).

Ties and errors: a request that errors counts as a failed request (`passes=0`) and is
disclosed verbatim; every objective, repeat and arm (including A) is reported, pass or fail. No
cherry-picking. Verdicts are reported plainly as PASS / KILL / NO_TOKEN_WIN / INCONCLUSIVE
with the numbers.

## 8. Anti-tuning rule

**One iteration is allowed ONLY for tooling/environment failures** — a crash, a timeout, an
OOM, a vLLM launch/context-length rejection, or a wiring bug in `evals/b4_divergent.py` /
`evals/mech_fabric.py` that prevents measurement. Such an iteration is disclosed exactly (what
changed and why) in a follow-up note. **Never** after seeing results: the file slice, ingester
rules, ownership map, objectives, required/forbidden facts, arm construction, or success
criteria are frozen as of this document. If the compile arm fails a criterion, it is reported
as failed; a new experiment would need a new, separately dated pre-registration.

## 9. Run environment (fixed now)

WSL2 Ubuntu, `~/.venvs/vllm`, run through `scripts/wsl_run_eval.sh` (its `REPO` is overridable
via `CF_REPO` so the eval runs from this worktree; the default is unchanged). The runner serves
`Qwen/Qwen2.5-1.5B-Instruct --enable-prefix-caching --max-model-len 8192 --enforce-eager
--gpu-memory-utilization 0.85`. GPU: RTX 5070 Laptop 8 GB (single GPU). The 4-file slice is
sized so every raw arm fits the 8,192 window (largest 7,970 tokens; dry-run asserts `< 8192`).

## 10. Artifacts (to be produced)

- `evals/reports/b4_divergent_<ts>.json` — full receipt (records, arms, cache, criteria
  verdicts, notes) for `--dry-run` and live runs (the receipt's `kind` distinguishes them; a
  live receipt is the only source of any PASS/KILL verdict). The dry-run receipt carries the
  recall diagnostic per objective.
- `tests/test_b4_divergent.py` — offline tests (no model, no network), including the ingester
  proof (objectives/keys excluded, determinism, source-derived blocks), the ownership-map
  divergence proof, the scorer, and dry-run determinism + recall.

Pre-stated caveats: single GPU; one 1.5B model; n = 12 objectives × 3 repeats = 36 requests per
arm; twelve short fact-style objectives (information-presence scorer with required+forbidden
facts, not a judge and not an executable test — declared in §4); warm-cache caveats apply only
within an arm (tags isolate arms); the first request of each arm is cold prefill.

## 11. Follow-up note — iteration 1 (tooling, disclosed under §8)

First live run (`evals/reports/b4_divergent_20261018_145719.json`, kept): 9/36 arm-B requests
(`implementer-*`, 3 objectives × 3 repeats) failed with HTTP 400 — vLLM rejected them for
exceeding `--max-model-len 8192`. Cause: §2/§9 sized prompts with tiktoken `o200k_base`; vLLM
counts with the Qwen2.5 tokenizer, which yields more tokens for this bundle (arm A averaged
~8,120 Qwen tokens per request vs ~7,940 tiktoken). This is a context-length rejection, the
tooling failure §8 names. Arms A and C had 0 errors. The first run's verdict (NO_TOKEN_WIN) is
therefore computed on a baseline arm with 25% of requests missing and is **not** the result of
record.

The single allowed iteration changes exactly one thing: the engine runs with
`--max-model-len 16384` (via `VLLM_MAX_LEN=16384` in `scripts/wsl_run_eval.sh`) for all three
arms. Fixture, ingester, ownership map, objectives, scorer, arms, order, `max_tokens`, repeats
and criteria are unchanged. The iteration-1 receipt is the result of record; no further
iteration is permitted.
