# v4 item 1: redesign the handoff test (design note, 2026-10-30)

Status: **proposal, needs founder approval before H3 freezes the protocol.** It changes the paper's primary outcome.
Covers item 1 of "Before H3" in `v4-pilot-notes.md`. Items 2 (hidden tests) and 3 (fork model) are settled or
folded in as W0 below; item 4 (carry-back) is out of v4.

## 1. Problem: what the pilot showed

Pilot `v4-pilot-1` (unregistered, 2 tasks x 5 arms x 1 rep, Sonnet parent, Haiku children, $1.39 total):

| Arm | Planted-fact pass | Child re-reads of parent-read files | Parent files read before `Agent` | Cost |
|---|---|---|---|---|
| `none` | 5/6 (0.833) | 1, 0 | 1, 1 | $0.128, $0.147 |
| `brief_v0` | 5/6 | 1, 1 | 1, 2 | $0.106, $0.172 |
| `brief_v1` | 5/6 | 0, 1 | 1, 1 | $0.126, $0.148 |
| `fork` (Sonnet child) | 5/6 | 1, 0 | 1, 0 | $0.286, $0.240 |
| `control` | 0/6 | 1, 1 | 1, 2 | $0.098, $0.154 |

(Pairs are `v4-option-sections`, `v4-command-aliases`. Source: `evals/reports/v4_pilot/v4-pilot-1.json`.)

Two findings:

1. **Explicit constraints are not a handoff failure.** The parent copies the "Constraints for this task" sentence
   into its delegation prompt, so `none` already passes. Brief v1 has nothing to add. This is a real result, not a
   test bug, and we report it as a null. We do **not** rig the parent to forget.
2. **There was almost no implicit context to hand off.** The parent read 0 to 2 files before delegating, so child
   re-reads could only be 0 or 1. Any re-read or child-cost metric on this design has no room to move.

The thesis says handoffs fail where the parent knows things it does not think to say: facts from long ago, files
it already read, things it already tried. The redesign tests that, and keeps the pilot design as a condition where
Brief is expected not to help.

## 2. Proposed design

### Two conditions, same tasks, same arms

| Condition | What the parent gets | Expected for Brief |
|---|---|---|
| **E (explicit)**, today's design | One prompt: task + constraints + delegation rules | No effect (reported null; the negative condition) |
| **D (distance + implicit)**, new | A 4-turn session: facts early, real work in between, delegation last | The thesis predicts fewer child re-reads, lower child cost, and no worse fact pass |

Arms stay `none`, `brief_v0`, `brief_v1`, `fork`, `control` in both. `control` in D gets the same session minus the
constraints sentence. `fork` stays in the per-arm table only (not model-matched; no cost ratio).

### How a D session is built with the existing harness

The harness already does multi-turn sessions: `build_command(..., resume=True)` emits `--resume <session_id>`, and
`_run_chain` (v2/v3) sends a list of user turns through one `--session-id` then `--resume` calls, with
`mcp_churn(..., invocations=n)`. v2 relied on this producing one parent transcript per session. So D needs no
pre-seeded transcript and no new mechanism, only a turn list in `_run_v4`. A pre-seeded transcript was rejected:
it would fake the parent's own reads, which is the thing we are measuring.

Turns come from **one fixed template** in `v4_prompt.py`, filled mechanically from fields already in
`tasks_v4.json` (task line, constraints sentence, `needs_read_files`). No per-task hand-written prose.

| Turn | Content (draft wording, Opus approves once, then frozen) | Purpose |
|---|---|---|
| 1 | "Next we will work on this change in Click: `<task line>`. Before we start, some decisions for later. `<constraints sentence>` Reply with OK; don't change anything yet." (`control`: constraints omitted) | Plants the facts early |
| 2 | "Read `<needs_read_files[0..1]>` yourself (no sub-agents) and explain how the code this change touches works today, with line numbers." | Parent builds implicit context: files read |
| 3 | "Read `<needs_read_files[2:]>` and run `python -m pytest -q <test file>`. Summarise what the existing tests cover and where the new code should go." | More distance; a test run the child could reuse |
| 4 | `<task line>` + today's delegation rules (no constraints) | Delegation. Facts now sit 3 turns and roughly 40-60k tokens back (estimate) |

Notes and risks:
- The parent may still restate turn-1 facts in its delegation prompt. That is allowed and is the honest test. If it
  does, D facts are a second null and the claim rests on implicit context only.
- Turn 1 contains "don't change anything yet", a stale constraint by turn 4. A real session has these. Brief v1
  extracts `don't` sentences as constraints, so it may carry it to the child and hurt. We keep it: removing it
  because we know the extractor's rules would tune the benchmark for Brief. Receipts will show whether it was carried.
- **Known Brief v1 weakness, found reading the code (not run):** intent items are ordered newest first
  (`cfxd/brief_intent.py`) and the Haiku constraints sub-budget is 12% of 5,200 = 624 bytes (ADR-0011). Turn 4's
  delegation rules contain `must` / `do not` / `Call the Agent tool...` sentences, which are newer than turn 1 and
  also classify as constraints. They may fill the sub-budget and push the planted facts out. See Q2.
- The parent may spawn sub-agents in turns 2-3 despite "yourself". Children spawned before turn 4 are tagged
  `prelude_spawn` and excluded from child metrics; a run with any is still scored and the count is reported.
- Parent auto-compaction before turn 4 would hide early records from Brief v1 (it reads after the last
  `compact_boundary`; tail cap 4 MB in `cfxd/checkpoint.py`). Record `parent_compactions`; not expected at ~60k tokens.
- Prompt caching across `--resume` calls is assumed, not verified. It drives the D cost estimate.

### Files to change

| File | Change |
|---|---|
| `evals/plugin_ab/v4_prompt.py` | `session_turns(prompt, arm, child_model, condition, needs_read_files) -> list[str]`; E returns today's single prompt |
| `evals/plugin_ab/runner.py` | `RunSpec`/`plan_v4` carry `condition`; `_run_v4` loops turns with `resume=index > 0`, `mcp_churn(invocations=len(turns))`; `--v4-condition {explicit,distance,both}`; new metrics (section 3); record `condition` |
| `tests/fake_claude_v4.py` | Accept non-final turns (read files, no edits); spawn only on the final turn |
| `evals/plugin_ab/analyze.py` | Split by condition; new primary and secondaries (section 5) |
| `evals/plugin_ab/PROTOCOL_V4.md` (new, at H3) | Preregistration text and thresholds |
| tests (`tests/test_plugin_ab_v4*.py`) | Turn construction, resume chain, metrics on synthetic transcripts |

`cfxd/brief_receipts.py` should not need changes. `tasks_v4.json` changes only if W0 finds broken tests.

## 3. Implicit-context measurement

All from transcripts already captured; no paths or text kept in records.

| Metric | Definition | Source today | Status |
|---|---|---|---|
| `child_rereads` | Distinct files a child read (`Read` tool_use) that the parent read before that child's `Agent` call (path_sha16) | `delegation_metrics` | Exists, all arms |
| `rereads_of_briefed` | Briefed files the child read anyway | R1 receipt `n_rereads_of_briefed` | Exists, brief arms only |
| `parent_read_not_briefed` | Child reads of parent-read files the packet left out | R1 receipt `n_parent_read_not_briefed` | Exists; in `none` it equals `child_rereads` (receipt is `disabled` but still joins) |
| `child_dollars` | Children's priced cost, dedup counted | **Missing.** `usage.subagents` has tokens but no dollars; `children[].tokens_total` is a raw sum incl. cache reads, not a cost | Add: price each child with `prices.json` |
| `child_turns`, `child_files_read` | As today | `delegation_metrics` | Exists |
| `child_search_calls` | Child `Grep`/`Glob` calls (re-discovery without `Read`) | **Missing** | Add, secondary only |
| `reread_overlap_lines` | Lines of a child `Read` range that overlap a parent `Read` range of the same file | **Missing** (`Read` inputs carry `offset`/`limit`) | Add, exploratory: a re-read of different lines is not waste |
| `fact_distance_tokens` | Parent context tokens between turn 1 and the first turn-4 `Agent` call | **Missing** | Add, descriptive (shows D really is "deep") |
| `prelude_spawns`, `parent_compactions` | As in section 2 | **Missing** | Add, validity checks |

"Already tried" is not measured as a metric: the only thing the parent tries in D is a test run, and a child
re-running tests is often correct. Reported descriptively at most.

## 4. Anti-rigging safeguards

1. **Order of operations.** Template wording, metrics, primary outcome, thresholds and analysis code are frozen
   (committed, SHA in the protocol) **before** any D run is seen. The re-pilot may change harness code only for
   bugs (crash, wrong turn sent, mis-tagged children), never because of an arm's numbers. Any change after the
   re-pilot is logged as a deviation, and a change to anything an arm sees forces a re-pilot of all arms.
2. **Brief code freeze.** Brief v1 is frozen at a commit before the re-pilot. Q2 decides whether the newest-first
   weakness is fixed first; either way the decision is made from the code, not from D results.
3. **Mechanical template**, one text for all 8 tasks, filled from existing task fields. Published in the protocol.
4. **Keep `none` and `control`** in both conditions; `control` must fail planted facts in D too (validity).
5. **Report the E null** in the paper's main results table, not a footnote.
6. **A condition where Brief should not help** (E), plus a predicted harm case (stale "don't change anything yet").
   If Brief "wins" in E, that flags a measurement problem, not a success.
7. **One preregistered primary.** Everything else is secondary and labelled so; no switching after unblinding.
8. **Intention to treat:** runs with no spawn, prelude spawns or cap hits are scored, not dropped (infra failures
   excluded as today, counted in `infra_excluded`).

## 5. Outcomes and what each result means

**Primary (condition D):** geometric mean over tasks of the paired per-task child-cost ratio `brief_v1` / `none`,
with the existing task-level bootstrap 95% CI. Superiority claim only if the CI upper bound is below 1.00.

**Guardrails (condition D, non-inferiority, existing rule `CLAIM_NON_INFERIOR_TASKS = 1`):** `brief_v1` fails at most
1 more task than `none` on (a) hidden behaviour tests, (b) planted-fact tests. Failing a guardrail voids a cost win.

**Secondary (labelled as such):** D `child_rereads` difference; D total cost ratio (parent + children; includes the
packet's own tokens); D planted-fact pass difference (superiority test if `none` drops facts); `brief_v1` vs
`brief_v0` on the same metrics; all metrics in E (expected null); `fork` descriptive only.

Why child cost and not total cost as primary: the D prelude is identical across arms and costs more than the
handoff, so total cost dilutes the effect the thesis is about. Child cost still includes the packet bytes Brief adds,
so a bloated brief shows up as a loss. Why not re-reads as primary: they are the mechanism, small integers, and
blind to a child that reads less but thinks longer. Cost is the outcome a user pays for.

| Result | Paper claim |
|---|---|
| **Win** (CI < 1, guardrails hold) | "When the parent has implicit context, Brief lowers sub-agent cost by X% at equal quality. For constraints the parent states, parents already brief well; Brief adds nothing (E null)." |
| **Null** (CI spans 1, guardrails hold) | "Sonnet parents brief Haiku children adequately on these tasks; Brief is cost-neutral." Still a paper: the measurement method, E and D nulls, fidelity/refetch motivation. The pitch line "Big models brief small models badly" is not supported and is dropped. |
| **Loss** (CI > 1, or a guardrail fails) | Published the same way. Brief's packet costs more than it saves, or carries stale constraints. Receipts say which. |
| D facts: `none` drops them, `brief_v1` keeps them | Strong secondary support for the thesis, even if cost is null |

Uncertainty: with 8 tasks the CI will be wide. Pilot child token totals ranged 146k to 520k on n=1 per cell, so only
a large effect (likely 30% or more, not computed) would clear 1.00. The re-pilot cannot size the sample; the
registered size is set by budget and the paper reports precision, not power.

## 6. Cost

Per-run D cost is an estimate, not measured: the prelude adds roughly 40-60k tokens of file content (`core.py`
alone is 148 KB) that every later parent turn re-reads from cache. Estimate **$0.40-0.60 per D run**, fork about
$0.90 (Sonnet child inherits the long context). E stays at about $0.14 (fork $0.26).

| Run | Cells | Runs | Estimate |
|---|---|---|---|
| **Re-pilot** (D only; E already piloted) | 2 pilot tasks x {`none`, `brief_v1`} + 1 x `control` | 5 | **$2.00-3.00**; per-run cap $0.90, stop at $3.00 |
| Registered D | 8 tasks x 5 arms x 2 reps | 80 | about $45 |
| Registered E | 8 tasks x 5 arms x 1 rep | 40 | about $7 |
| **Registered total** | | 120 | **about $50 (range $40-70)** |

Dropping `brief_v0` and `fork` from registered D saves about $23. The re-pilot skips `fork` and `brief_v0` to stay
in budget; their D behaviour is first seen in the registered run (risk accepted; they are not primary).

## 7. Work packages and order

| WP | What | Files | Acceptance | Opus decision |
|---|---|---|---|---|
| W0 | Verify every hidden and planted test against `fixture_v4_reference` (free) | `fixture_v4_hidden/*`, maybe `tasks_v4.json` | Reference passes all; bare fixture fails behaviour and planted; reference variant fails planted | Drop or fix a test |
| W1 | Brief v1 offline check on a synthetic D transcript (free, no `claude`) | none (scratch script) | Report which turn-1 facts survive into a Haiku packet | Yes: feeds Q2 |
| W2 | D turn template | `v4_prompt.py`, its tests | Facts only in turn 1; final turn = task line + rules; `control` has no facts anywhere; same template for all tasks | Yes: approve wording |
| W3 | Resume chain in `_run_v4`, `condition`, CLI, fake support | `runner.py`, `tests/fake_claude_v4.py`, tests | Fake end-to-end passes for 5 arms x 2 conditions; one parent transcript; `mcp_churn` 0; prelude spawns tagged | No |
| W4 | New metrics (section 3) | `runner.py` (`delegation_metrics`), tests | Unit tests on synthetic parent/child transcripts for each metric | No |
| W5 | Analysis split + new primary/guardrails | `analyze.py`, tests | Fake records reproduce hand-computed ratios; E and D reported separately | Yes: thresholds |
| W6 | Re-pilot (5 runs, ≤ $3) | `v4-pilot-notes.md` | Gates: spawn on turn 4 in every run; no prelude spawns or counted; `control` fails D facts; `fact_distance_tokens` > 30k; D cost measured | Read transcripts |
| H3 | Freeze protocol (SHA) | `PROTOCOL_V4.md`, `prereg.py` | Founder sign-off | Founder |

Order: W0 and W1 in parallel (free) → founder answers Q1-Q3 → W2 → W3 → W4 → W5 → W6 → H3. W3 and W4 both touch
`runner.py`, so they run in sequence.

## 8. Open questions for the founder

- **Q1. Change the primary outcome** from "total cost at equal pass rate, planted facts as the quality check" to
  "child cost in condition D, with hidden-test and planted-fact pass as non-inferiority guardrails", and report E as
  a null? This narrows the claim to "Brief helps when the parent has implicit context". *Recommendation: yes.* It is
  the only version of the test with room to show an effect, and E keeps the honest null in view.
- **Q2. Brief v1's newest-first constraint ordering** may drop deep facts in favour of the harness's own delegation
  rules. Fix it before the freeze (decided from W1's offline check, before any D run), or test v1 as shipped?
  *Recommendation: decide on W1 alone; fix only if W1 shows the planted facts are dropped, and record the fix and
  its reason in the protocol before the re-pilot.* Fixing after seeing D results would be rigging; shipping a
  known, code-level bug is a weaker test of the idea.
- **Q3. Registered size:** 120 runs, about $50 (range $40-70, estimate from an unmeasured D cost), with 2 reps in D.
  *Recommendation: approve up to $70, decide 2 vs 3 reps after the re-pilot measures D cost; if the cap binds, drop
  `brief_v0` from D before cutting reps.*
