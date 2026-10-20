# B4 Exploration — why compile lost, and which levers recover quality

**Status: EXPLORATORY. Not a result of record.** The B4 v2 objectives are now a **dev set**
(`order-b/b4-explore`): every number below is a tuning measurement on objectives we are
allowed to over-fit. Nothing here can be claimed as a result; a later, separately
pre-registered **B4 v3** on held-out objectives (written by a different agent) must confirm.

Reference points (frozen): `docs/engineering/B4-RESULT.md`,
`docs/engineering/B4-PREREGISTRATION-v2.md`, `evals/reports/b4_divergent_20261018_145937.json`.
Ablation receipts: `evals/reports/b4x_20261018_162157.json` (the clean run; an identical
first run `b4x_20261018_161616.json` reproduced the same numbers byte-for-byte).

---

## STEP 1 — failure analysis: why the v2 compile arm lost (21/36)

Method: for each of the 15 failed compile requests in the v2 receipt, classify the cause.
"Fact present" is checked by **exact token match** against the role's compiled packet
(rebuilt offline with `evals/mech_fabric.build_blocks` + `compile_node_packet`), not by the
substring the scorer uses.

**Recall result (headline): every required fact is present in its compiled packet as an
exact token — 12/12 objectives, 100%.** The v2 dry-run "recall" table already showed this;
re-checking with exact-token match confirms it (e.g. `is_error` is a distinct token in the
`class codes` method list, not merely a substring of `is_server_error`). The compile arm
does **not** drop facts. It drops the *associations* and the *formatting discipline* that
let a 1.5B model turn a fact token into the right answer.

| # | objective | v2 failure cause | required present? | root cause |
|---|---|---:|---|---|
| planner-1 (×3) | import of `Client`,`Response` | (a) truncation — verbose answer hit `max_tokens=48` mid-`_models` | yes (`_client`,`_models` in import list) | import block lists root names only; no `from ._models import Response` mapping; model guesses |
| planner-2 (×3) | import of `DEFAULT_TIMEOUT_CONFIG`,`URL` | (c) wrong pick — answered `_client,_types` | yes (`_config`,`_urls` in list) | same lossy import representation; model cannot map symbol→module |
| implementer-2 (×3) | `FunctionAuth`,`NetRCAuth` | (c) wrong pick — echoed `class, NetRCAuth` | yes | "Answer `class, class`" template confusion; verbose answer also truncates in the ablation run |
| implementer-3 (×3) | `DigestAuth` | (a) truncation — verbose answer cut at `` `Digest `` | yes | verbosity beats `max_tokens=48` |
| tester-3 (×3) | `is_error` classmethod | (c) wrong pick — answered `is_server_error` | yes (token present) | method *names* are in the class signature, but not their bodies/docstrings; `is_error` vs `is_client_error` vs `is_server_error` are indistinguishable |

**The 15 v2 failures are: 6 truncation (a), 9 wrong-pick (c), 0 recall miss (b), 0 scorer
artifact (d).**

### The set is not stable (measured, important)

The ablation harness re-ran the frozen `compile` arm with the same model, `temperature=0`,
`max_tokens=48` and an identical packet body, differing only in the leading cache-isolation
tag (`B4x::compile::48::` vs the v2 `B4::compile::`) and the arm order. The **total** is
stable (21/36, reproduced byte-for-byte across two runs), but the **individual objectives
shuffle**:

- v2's failures: planner-1, planner-2, implementer-2, implementer-3, tester-3.
- ablation run's `compile:48` failures: planner-1, planner-2, implementer-2, implementer-3,
  **reviewer-2** (tester-3 passed 3/3).

The flip exposes a fourth cause the v2 run did not hit:

| objective | cause | evidence |
|---|---|---|
| reviewer-2 (×3 in the ablation run) | **(d) scorer artifact** | model answered `request_content, response_content` — semantically correct (`RequestContent`/`ResponseContent`) but snake_case, so the substring scorer finds neither `requestcontent` nor `responsecontent` |

**Why this matters:** with a 1.5B model at `temperature=0`, the answer to a borderline
fact question can flip on the leading tag bytes. Each 3-request objective is 8% of the
score, so a one-objective flip moves quality by ±3. Conclusions must therefore be drawn
from the **totals** (21 vs 24 vs 30), not from any single objective's attribution.

---

## STEP 2 — research notes (levers we can build; 1 page)

**Verified** (fetched 2026-10-18):

- **aider repository map** (`https://aider.chat/docs/repomap.html`): sends the LLM "the most
  important classes and functions along with their types and call signatures" **plus "the
  critical lines of code for each definition"**; ranks files by a graph algorithm (edges =
  dependencies, i.e. PageRank) and fits the map into a token budget (`--map-tokens`, default
  1k). The "critical lines", not just signatures, is exactly what our mechanical fabric
  lacks (L2 lever).
- **aider prompt caching** (`https://aider.chat/docs/usage/caching.html`): caches the system
  prompt, read-only files, **the repo map**, and the editable files as a stable prefix. The
  repo map sits in a byte-stable leading position so it is shared across every turn — the
  same "Tier-0 shared head" our `sidecar/render.py` already renders first (L1 lever).

**In-repo, verified by reading the code**:

- `sidecar/render.py` deliberately renders Tier-0 first *because* cross-agent KV sharing
  needs identical leading bytes; `kernel/router/compiler.py` only pins a cartridge as Tier 0
  when a `Cartridge` is passed — the B4 v2 compile arm passed none, so its 12 distinct
  packets share **zero** leading bytes (this is the whole uncached-token loss).
- `kernel/router/packer.py` lambda-MMR selection (λ=0.7) drops lowest-utility tier-3 blocks
  under budget; `kernel/router/scorer.py` weights facts by lexical overlap.

**Unverified (memory, not re-fetched this session)**:

- LLMLingua / LongLLMLingua report that aggressive prompt compression can drop exactly the
  low-frequency tokens a factual Q&A needs; the lesson is *lossy-by-default* compression is
  dangerous for information-presence tasks, consistent with B4's result.

**Levers that survive this read-through** (all mapped to code in STEP 3):

1. **Shared repo-map prefix (L1)** — pin the cartridge hologram as Tier 0 so all roles share
   leading bytes (aider's cached repo map). Token lever only; no quality change expected.
2. **Query-aware richer blocks (L2)** — keep full `from X import Y` statements (aider's
   "critical lines"), and full bodies for the top-k objective-relevant symbols.
3. **Answer-format contract (L3)** — a terse output contract; the frozen contract
   ("reply with the required structured output only") is not strong enough to stop a 1.5B
   model from rambling into truncation.
4. **Budget sweep (L4)** — packet budget as the token/quality dial.

---

## STEP 3 — ablation results (dev set, one lever at a time vs the v2 compile arm)

Same model (`Qwen/Qwen2.5-1.5B-Instruct`), `temperature=0`, non-streaming, `repeats=3`
(n = 36/arm), one vLLM session, arm tags isolate the prefix cache. `prefix` is the re-run
arm-B criterion baseline. Receipt: `evals/reports/b4x_20261018_162157.json`.

| arm | lever | quality | billed tokens | uncached tokens | peak KV |
|---|---|---:|---:|---:|---:|
| prefix:48 | B reference (prefix-cache-only) | **30/36** (0.83) | 293,223 | 8,903 | 6.6% |
| compile:48 | v2 compile (frozen) | 21/36 (0.58) | 41,316 | 13,860 | 1.7% |
| compile:96 | v2 + `max_tokens=96` | 21/36 (0.58) | 41,316 | 13,860 | 1.7% |
| l1:48 | shared Tier-0 cartridge | 21/36 (0.58) | 41,244 | **10,828** | 1.7% |
| l2:48 | full imports + top-k bodies | 21/36 (0.58) | 58,821 | 19,637 | 1.7% |
| l3:48 | terse answer contract | **24/36** (0.67) | 41,784 | 13,960 | 1.6% |
| l3:96 | terse + `max_tokens=96` | **24/36** (0.67) | 41,784 | 13,960 | 1.6% |
| l4_1k:48 | budget 1k | 21/36 (0.58) | 21,225 | 7,145 | 0.6% |
| l4_2k:48 | budget 2k | 22/36 (0.61) | 35,034 | 11,706 | 1.1% |
| l5:96 | l1 + l2 + l3 | 24/36 (0.67) | 61,164 | 17,500 | 1.7% |

Per-objective deltas (repeats out of 3, vs `compile:48`):

| objective | compile:48 | l1 | l2 | l3 | l5 |
|---|---:|---:|---:|---:|
| planner-1 (import map) | 0 | 0 | **3** | 0 | 0 |
| planner-2 (import map) | 0 | 0 | **3** | 0 | 0 |
| implementer-3 (truncation) | 0 | 3 | 0 | **3** | **3** |
| reviewer-2 (scorer artifact) | 0 | 3 | 0 | **3** | **3** |
| tester-3 (method semantics) | 3 | 0 | **0** | **0** | 0 |
| reviewer-1 (aliases) | 3 | 3 | **0** | 3 | 3 |

### What the ablations establish (measured)

- **L1 (shared prefix) is a pure token win, exactly as designed:** quality flat at 21/36,
  uncached drops 13,860 → **10,828 (−22%)**. It still does not beat prefix-cache-only's
  8,903, but it is the prerequisite for any cross-role sharing and costs nothing.
- **L2 (richer blocks) is zero-sum and token-expensive.** Full `from … import …` statements
  fix planner-1/planner-2 (+6 requests), but full source bodies backfire: the ~1.5k-token
  `class codes` body makes the model enumerate *all* `is_*` methods (tripping the forbidden
  `is_client_error` on tester-3) and the added bulk evicts `_types.py` alias blocks
  (reviewer-1 → 0). Net quality 21/36, billed +42%, uncached +42%.
- **L3 (terse contract) is the only quality win:** +3 (21 → 24). It stops rambling into
  truncation (implementer-3 → `DigestAuth`) and stops the snake_case re-render (reviewer-2),
  but it cannot conjure the missing association (planner-1/2 stay 0) and makes tester-3
  tersely wrong (`is_server_error`). `max_tokens=96` adds nothing once the contract is
  terse; `max_tokens` alone only fixes the truncation subset, not the total.
- **L4 (budget) trades tokens for nothing:** 1k = 21/36, 2k = 22/36. Smaller packets save
  billed/uncached/KV but do not recover quality.
- **L5 (best combo) = L3, no synergy** (24/36): l1+l2+l3 does not exceed l3 alone, and pays
  l2's token cost for it.

### Conclusion: why compile lost, stated precisely

Compiled context loses on quality because the mechanical fabric compresses **symbols to
names** (import roots, method-name lists) and **contracts to a weak instruction**, then
asks a 1.5B model to re-derive the dropped associations. On a fits-in-context workload it
also loses on uncached tokens because 12 role-distinct packets share zero leading bytes
(no Tier-0 pin) while prefix-cache-only shares one 8k bundle. The levers above each repair
one axis — L1 the tokens, L3 the formatting, L2's *import half* the mapping — but **none
recovers the quality gap** (best 24/36 vs prefix 30/36, broadcast 35/36). The residual gap
is the model's inability to disambiguate same-shaped symbols (`is_error` vs
`is_server_error`) from names alone, plus scorer false-negatives on formatting.

---

## Proposed B4 v3 design (levers chosen and why — objectives NOT written here)

The v3 objectives are to be authored separately by a different agent on held-out content.
This section fixes only the *mechanism*, which is the experiment agent's remit.

1. **Pin the cartridge as Tier-0 (L1) — keep.** Zero quality cost, −22% uncached, and it is
   the honest shared-prefix structure. A v3 that omits it re-measures a strawman.
2. **Keep full `from … import …` statements, drop full bodies (L2, halved).** The import
   mapping recovered planner-1/2; the full symbol bodies were net-negative (noise +
   forbidden-token trips) and token-expensive. v3's fabric should carry **exact import
   statements** and, for classes, **method one-line docstrings/bodies** (not the whole
   class) — targeted at the "which method does X" gap without the bulk.
3. **Terse answer contract (L3) — keep, as the default.** +3 quality for free. v3's packet
   contract should demand the exact answer values and forbid prose.
4. **Fix the scorer's false-negative, or accept it as stated.** The substring conjunctive
   check punishes `request_content` for `RequestContent`. A v3 scorer should match on
   **token-level** presence (split on `[a-z0-9]+`, underscore as separator) so snake/Pascal
   renderings of the same identifier both count. (This is a scorer change, so it must be
   pre-registered as part of v3, not retro-applied to v2.)
5. **Report totals, not single-objective attribution.** vLLM greedy decode flips borderline
   objectives on the leading tag; v3's success criterion should be stated on the n=36 total
   with the tolerance the preregistration already uses, and v3 should freeze one tag/order.
6. **Open question to resolve before v3:** the "same-shaped symbols" gap (tester-3) is not
   fixed by any lever here. Candidate directions for the v3 fabric (to pre-register, not
   decide here): include method docstrings/bodies for class-methods (not the whole class),
   or rely on a larger model. The dev set alone does not resolve it.

---

## Reproduction

```
MSYS_NO_PATHCONV=1 wsl -d Ubuntu -- bash -c "VLLM_MAX_LEN=16384 CF_REPO=/mnt/c/dev/cf-b4-explore CF_B4_FIXTURE=/mnt/c/Users/<user>/AppData/Local/Temp/opencode/httpx_pilot bash /mnt/c/dev/cf-b4-explore/scripts/wsl_run_eval.sh evals.b4_explore"
```

Levers are implemented behind flags in `evals/b4_explore.py` (default behaviour of
`evals/b4_divergent.py`, `evals/mech_fabric.build_blocks`, `compile_packet`/`pack` and
`compile_node_packet` is unchanged); see `tests/test_b4_explore.py`.
