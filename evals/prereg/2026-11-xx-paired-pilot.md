# Pre-registration: Paired Boundary-Restart Pilot and Confirmatory Run

**Status: NOT REGISTERED until the founder commits the probe result.**

This pre-registration draft is committed before any paid benchmark run. Per founder decision D-4 (`docs/plans/2026-11-01-wave-decisions.md`, frozen at commit `b6f1c26`), no budget ceiling is chosen blind: the founder first approves and runs the 3-task calibration probe per arm on Haiku (authorised up to $10.00 total), commits the observed probe cost and computed ceiling into Section 6 below, and locks the protocol hash before the paired pilot begins. No context or cost reduction is claimed until a registered paired run clears every threshold and guardrail in this document.

---

## 1. Scope and Eligible Mechanism

Following the Wave 1–2 offline measurements (`docs/plans/2026-11-02-p4-results.md`, bars frozen in `docs/plans/2026-11-01-wave-decisions.md` at `b6f1c26`) and `docs/decisions/ADR-0013-retire-bound-and-checkpoint-hold-carry-v2.md`, **the only conservation mechanism eligible for paired testing is a manual or coach-prompted restart at deterministic task boundaries using the lean working-set carry packet (P7)**:

- **Backward dedupe (P5 / W3-03) is demoted and excluded:** In P4 Measurement 1 (`docs/plans/2026-11-02-p4-results.md`), exact tool-result repeats measured `0.1%` of new tokens (`1.3%` of landed tool-result bytes) and near-duplicates measured `0.0%` of new tokens (`0.4%` of landed tool-result bytes) across 1,543 transcripts (*measured*). Both shares sit below the frozen `< 5%` demotion floor (`b6f1c26`). P4 Measurement 2 confirmed that full `Read` re-reads are only `3.6%` of `Read` bytes. Per the frozen bar rules, backward dedupe is parked for the plugin and moved to the framework track only.
- **Retired mechanisms remain excluded (ADR-0012, ADR-0013):** `fold`, `bound`, and fixed-step `checkpoint` are retired from the tester surface. Fixed-step `checkpoint` measured `1.25x` (long) and `1.37x` (medium) the cost of `shipped` once all sessions of a run were counted (`docs/deviations/2026-10-30-v2-mcp-race-and-double-count.md`). `carry_v2` remains off by default after missing the Gate 0 E-4 per-kilobyte bar.
- **Brief v1 is excluded from conservation claims:** `Brief v1` remains an opt-in delegation experiment (`11.8%` precision in replay) with no tester saving claim.
- **Deterministic boundary restart (P7) cleared Gate 1 (P4 M3):** In P4 Measurement 3 (`docs/plans/2026-11-02-p4-results.md`), `18 of 44` (`40.9%`, *modelled*, one machine) sessions of 40+ turns had a deterministic boundary candidate with a break-even turn and positive net headroom, clearing the frozen D-2 bar (`>= 25.0%`). Gate 2 is the Wave 4 coach randomised-control trial (P11): if the coach treatment arm demonstrates a behaviour difference, P7 is tested as a coach-prompted boundary restart; otherwise, P7 is tested as a manual boundary-restart command at deterministic task boundaries.

---

## 2. Lessons Encoded from the Voided v2 Run

The v2 registered run (`evals/plugin_ab/PROTOCOL_V2.md`) was voided after post-run audit uncovered three measurement and harness defects (`docs/deviations/2026-10-30-v2-mcp-race-and-double-count.md`). This protocol encodes mandatory controls for all three:

1. **Identical MCP and plugin-loading state across all arms (fixes Defect D1 — MCP connector race):**
   - In v2, the `off` arm launched without the plugin while account MCP connectors were still initialising; `off` started turn 1 before connectors finished attaching and therefore paid a `deferred_tools_delta` cache rewrite on nearly every `--resume` step (14–27 attachments per long run in `off` vs 1 in `shipped`), whereas `shipped`'s `SessionStart` hook delayed turn 1 just enough for connectors to settle once.
   - **Rule:** Every arm in this study runs with **identical MCP isolation** (account-level connectors blocked; `--setting-sources project,local` plus isolated test home/config) and **identical plugin-loading state** (`cfxd` daemon and hooks loaded in both control and treatment; control is `shipped` with boundary restart disabled, rather than an unloaded `off` arm).
   - **Hard assertion:** The runner inspects every session transcript in every arm and asserts `deferred_tools_delta <= 1` per session. Any run with `deferred_tools_delta > 1` in any session is flagged immediately as an infrastructure failure (`mcp_churn`).
2. **Usage de-duplicated per `message.id` across ALL sessions of a run (fixes Defects D2 and D3):**
   - **Once-per-`message.id` counting (D2):** Claude Code writes one JSONL transcript line per assistant content block and repeats the message's `usage` payload on every line. All token and cost accounting must use `cfxd/usage_dedupe.py` (`evals/plugin_ab/usage.py`), counting each `message.id` exactly once.
   - **All-session completeness across restarts (D3):** When the boundary-restart arm takes a restart boundary, it starts a fresh `--session-id`, producing multiple session JSONL files for a single task chain. In v2, the runner failed to reset or carry cumulative totals across session ID changes, silently dropping every session before the final one (`dedup, final session` vs `dedup, all sessions`). Here, the runner and analyzer must enumerate and sum de-duplicated usage across **every session transcript created during the task chain** (every pre-restart session, every post-restart session, and any sub-agent transcripts), verified by a per-session accounting audit in the run record.
3. **Stable, unformatted fixture:**
   - Every task runs against a fresh, isolated working copy of the pinned fixture tree (`evals/plugin_ab/fixture_v2/` and its locked task extensions). Per `AGENTS.md` R5, no formatter is ever run over `evals/plugin_ab/fixture_v2/`. The fixture directory contents, task definitions, and price table are SHA-256 hashed into the registration manifest before the run starts.

---

## 3. Task Set and Staged Design

Tasks are multi-step scripted coding chains on the stable shop/checkout fixture (`fixture_v2/`), where each step is dispatched via `claude -p` (`--session-id` on step 1 and after a boundary restart; `--resume` on continuing steps) and verified by its own deterministic `pytest` oracle. Unlike v2's arbitrary fixed step indices, candidate restart boundaries in these chains correspond to **deterministic task-boundary signals** defined in P6/P7:
- a failing test suite transitioning to passing (`pytest` exit 0 after a prior non-zero exit),
- a completed commit boundary between feature stages, or
- an explicit user task-switch prompt step.

Each chain concludes with a deterministic **recall and fidelity probe** (`answers.json` plus a post-restart tool-call audit checking whether the agent re-read files or re-asked constraints already present in the carry packet).

### Stages

| Stage | Size | Purpose | Included in confirmatory claims? |
| --- | --- | --- | --- |
| **Stage 0: Calibration probe** | 3 tasks × 2 arms (6 cells) | Verify identical MCP state (`deferred_tools_delta <= 1`), multi-session usage summation, boundary trigger mechanics, and measure `probe_cost_per_task` on Haiku 5.5 to lock the D-4 budget ceiling. | **No** (calibration only) |
| **Stage 1: Paired pilot** | 20–30 tasks × 2 arms | Evaluate boundary-restart failure modes, carry-packet truncation under the 3 KB cap, post-restart re-discovery rate, and the -5 pp non-inferiority guardrail. | Pilot report (*measured*, labelled pilot) |
| **Stage 2: Confirmatory paired run** | 100+ tasks × 2 arms | Full-scale confirmatory evaluation (proceeds only if the Stage 1 pilot clears all guardrails and kill criteria, under a fresh D-4 ceiling locked before Stage 2 starts). | **Yes** |

Within each stage, tasks are stratified by chain length:
- **Medium chains (4–6 steps, 1 deterministic boundary):** tests whether a boundary restart breaks even on moderate chains or fails to amortise the prefix re-write.
- **Long chains (8–12 steps, 2 deterministic boundaries, targeting 40+ assistant turns):** the target population from P4 M3 where `40.9%` (*modelled*) of sessions showed positive restart headroom.

---

## 4. Arms and Pairing Protocol

### Registered arms

1. **`shipped` (Control):**
   - `cfxd` plugin loaded with default tester settings (`brief` off, `carry_v2` off, `dedupe` off).
   - Identical `SessionStart` / `PostToolUse` hook path and MCP isolation as the treatment arm.
   - No boundary restart is taken: step 1 uses `--session-id <uuid>` and every subsequent step uses `--resume <uuid>` in a single continuous session.
2. **`restart` (Treatment — P7 deterministic boundary restart):**
   - Identical plugin binary, hook registration, and MCP isolation as `shipped`, with the pure working-set tracker (`cfxd/working_set.py`, P6) and P7 boundary-restart policy enabled.
   - A boundary restart is triggered after step $k$ **if and only if** (a) step $k$ completes a deterministic task boundary (test pass after failure, commit, or explicit task switch) **and** (b) the P4 M3 break-even check indicates positive expected headroom for the remaining chain given the session's measured turn-1 prefix and current context size.
   - When triggered, step $k+1$ starts a **fresh session** (`--session-id <new_uuid>`, no `--resume`) with the lean working-set carry packet rendered from `WorkingSet` (hard cap `<= 3,000` bytes; constraints and decisions populated strictly from user/parent messages per `AGENTS.md` R10, never tool output).
   - Every run record logs: boundaries eligible, boundaries taken, carry packet byte size, session IDs created, and per-session de-duplicated token counts.

### Pairing and execution order

- **Paired design:** Every task runs in both `shipped` and `restart` with the identical pinned model (`claude-haiku-5-5`), prompt sequence, seed (`--seed 0`), and fresh working copy.
- **Order:** Seeded ABBA counterbalancing (`shipped`, `restart`, `restart`, `shipped`) across task repetitions so time-of-day or API latency drift affects both arms equally.
- **Repetitions:**
  - Probe (Stage 0): 3 tasks × 2 arms × 1 run per cell = 6 runs (or up to 1 warm-up + 2 measured reps = 18 runs if under the $10.00 probe cap).
  - Pilot (Stage 1, 20–30 tasks): 1 warm-up pair on task 1 (recorded, excluded from analysis) + 1 to 3 measured paired repetitions per `(task, arm)` cell, fixed when locking the probe result.

---

## 5. Guardrails, Claim Thresholds, and Kill Criteria

### Primary non-inferiority guardrail (-5 pp task success)

- **-5 pp Non-Inferiority Guardrail:** Across all measured tasks in the run, the step and task resolution rate of `restart` must not be worse than `shipped` by more than **5 percentage points (-5.0 pp)**:
  $$\text{PassRate}(\text{restart}) - \text{PassRate}(\text{shipped}) \ge -0.050$$
  In addition, the lower bound of the one-sided 95% paired bootstrap confidence interval on $\Delta \text{PassRate} = \text{PassRate}(\text{restart}) - \text{PassRate}(\text{shipped})$ must not fall below `-0.050` in the confirmatory run. If `restart` violates the -5 pp guardrail, **no conservation or cost-reduction claim may be made**, regardless of token or dollar ratios.

### Claim thresholds (frozen before any run)

| Metric | Medium stratum | Long stratum (primary) | Notes |
| --- | --- | --- | --- |
| **Primary cost ratio (`restart / shipped`)** | `<= 0.90` (exploratory check) | **`<= 0.85`** with 95% CI upper bound `< 1.00` | Geometric mean of per-task `restart / shipped` cost ratios across all sessions of each run, re-priced at Opus 5.5 (`evals/plugin_ab/prices.json`). |
| **Limit-weighted token ratio (`limit-weighted/1`)** | Reported with sensitivity | **`<= 0.85`** with 95% CI upper bound `< 1.00` | Evaluated at default `w_read = 1/67` and sensitivity points `w_read = 1/30, 1/100` (P8). |
| **Task success non-inferiority** | $\Delta \text{PassRate} \ge -5.0\text{ pp}$ | **$\Delta \text{PassRate} \ge -5.0\text{ pp}$** | Paired pass-rate difference (`restart - shipped`). |
| **Recall probe retention** | Drop `<= 5.0 pp` vs `shipped` | **Drop `<= 5.0 pp` vs `shipped`** | End-of-chain 3-fact `answers.json` oracle score. |
| **Post-restart fidelity (re-discovery)** | Reported | Re-read share of carry-listed files `<= 15%` | Share of edited/read files named in the carry packet that the agent re-reads in the first 3 post-restart turns. |

### Kill criteria

If any of the following holds in the pilot or confirmatory run, P7 boundary-restart claims are halted and the negative result is published as written:
1. Long-stratum `restart / shipped` all-session re-priced cost ratio `> 0.95` (no meaningful headroom once real post-restart re-orientation turns are paid).
2. Task resolution rate in `restart` drops by more than `5.0 percentage points` (`< -5.0 pp`) relative to `shipped`.
3. Recall probe score drops by more than `10.0 percentage points` relative to `shipped`.

---

## 6. Budget, Ceiling Formula (D-4), and Stop Rule

### Probe budget authorisation

- The founder authorised **at most $10.00 TOTAL** in actual API spend for the Stage 0 3-task probe across all arms on `claude-haiku-5-5` (`docs/plans/2026-11-01-wave-decisions.md`).
- No probe or pilot run executes during Wave 3 doc drafting; the probe runs only after founder sign-off.

### Ceiling formula (Decision D-4)

Once the 3-task probe completes on `claude-haiku-5-5`, let `probe_cost_per_task` be the observed mean actual Haiku spend per `(task, arm)` cell across the 3 probe tasks (including all sessions of each chain and all repetitions per cell). The hard spending ceiling for the subsequent run is:

$$\text{ceiling} = \text{probe\_cost\_per\_task} \times \text{tasks} \times \text{arms} \times 1.5$$

### Worked example (placeholder probe costs — illustrative only, not observed data)

> **Note:** The numbers below use **placeholder** values solely to illustrate the arithmetic required by D-4. Do not cite them as observed costs. Real probe figures replace the `TBD` table below when the founder commits the probe result.

- Suppose the 3-task probe runs on `claude-haiku-5-5` across `arms = 2` (`shipped`, `restart`) and observes a placeholder mean cost of **`probe_cost_per_task = $0.12` per `(task, arm)` cell** (so the 3-task × 2-arm probe itself cost $3 \times 2 \times \$0.12 = \$0.72$, well inside the $10.00 total probe authorisation).
- Applying the D-4 formula to a **Stage 1 paired pilot** with `tasks = 25` and `arms = 2`:
  $$\text{ceiling}_{\text{pilot}} = \$0.12 \times 25 \times 2 \times 1.5 = \$9.00$$
- Applying the same formula to a **Stage 2 confirmatory run** with `tasks = 100` and `arms = 2` (using the same placeholder `$0.12` per cell):
  $$\text{ceiling}_{\text{confirmatory}} = \$0.12 \times 100 \times 2 \times 1.5 = \$36.00$$

### Registration lock table (filled by founder after Stage 0 probe)

| Field | Value |
| --- | --- |
| Registration status | **NOT REGISTERED until the founder commits the probe result** |
| Probe commit hash | `TBD` |
| Probe total Haiku spend (must be `<= $10.00`) | `TBD` |
| Observed `probe_cost_per_task` (USD per task-arm cell) | `TBD` |
| Planned pilot `tasks` / `arms` | `TBD` (target 20–30) / `2` (`shipped`, `restart`) |
| Locked pilot `ceiling` (`probe_cost_per_task × tasks × arms × 1.5`) | `TBD` |
| `prereg` manifest SHA-256 | `TBD` |

### Stop rules

1. **Budget ceiling stop rule (D-4):** The runner accumulates actual USD spend across all sessions after each completed run. If cumulative spend reaches `ceiling` (or if the next run would exceed `ceiling` based on `EVALS_MAX_RUN_USD`), **the runner stops immediately, writes the stop reason (`ceiling_reached`), and reports partial results**. No silent overrun is permitted.
2. **Per-run circuit breaker:** `EVALS_MAX_RUN_USD` (default `$0.60` on Haiku) remains enforced on every chain across the sum of its sessions; a chain that crosses `EVALS_MAX_RUN_USD` halts after that step and records `cap_hit` (counted as an unresolved task in pass-rate metrics, never silently dropped).
3. **Infrastructure stop rule:** Stop the run immediately if **two consecutive real-model runs fail infrastructure checks** (`claude` process launch failure or timeout, missing/unreadable JSONL transcript, step oracle execution crash, or `deferred_tools_delta > 1` MCP churn).
4. **No early stopping for a favourable trend:** The run never stops early because `restart` appears ahead of `shipped`.

---

## 7. Analysis Plan

### Exclusion rules (decided blind to arm)

A run is excluded from cost-ratio aggregation **only** for an objective infrastructure failure verified by script before inspecting cost or pass/fail outcomes:
- `claude` failed to launch or hit a process/network timeout,
- a session transcript file is missing or unreadable JSONL,
- the test oracle harness itself failed to execute, or
- `deferred_tools_delta > 1` occurred in any session of the run (`mcp_churn`).

A run that executed normally where the model wrote failing code, missed a boundary, or exhausted turns after a restart is **valid data, not an exclusion**. If a task has an infrastructure-excluded repetition, only complete paired cells are used in paired ratio estimators, and the exclusion count is reported by arm.

### Estimators and pricing

`evals/plugin_ab/analyze.py` computes the following pre-registered quantities per stratum (`medium`, `long`) and overall:

1. **All-session de-duplicated token accounting:**
   - For each run $r$, let $S(r)$ be the set of all session transcripts produced during $r$ (initial session, every post-restart session, and any sub-agent sessions).
   - Within each session $s \in S(r)$, assistant usage records are deduplicated once per `message.id` via `cfxd/usage_dedupe.py`.
   - Total run usage is the sum across all $s \in S(r)$ of `input_tokens`, `cache_creation_input_tokens` (5m and 1h), `cache_read_input_tokens`, and `output_tokens`.
2. **Re-priced dollar cost and limit-weighted tokens:**
   -Measured runs execute on **pinned `claude-haiku-5-5`**. Because Haiku 5.5 pricing steps up 5× above 100K prompt tokens (`evals/plugin_ab/prices.json`), raw Haiku bills inflate long-context costs and are **never used for claims**.
   - Every run's exact de-duplicated token counts are priced using the dated **Opus 5.5** (primary) and **Sonnet 5.5** (co-reported) rows of `evals/plugin_ab/prices.json`, and converted to **`limit-weighted/1` new-token equivalents** (`cfxd/limit_metric.py`) at `w_read = 1/67` with sensitivity at `1/30` and `1/100`.
3. **Geometric mean of paired per-task ratios and hierarchical bootstrap CI:**
   - For each task $i$, compute the mean cost $\bar{C}_{i,\text{restart}}$ and $\bar{C}_{i,\text{shipped}}$ over measured repetitions, and the task ratio $R_i = \bar{C}_{i,\text{restart}} / \bar{C}_{i,\text{shipped}}$.
   - The point estimate is the geometric mean across tasks: $\hat{R} = \exp\left(\frac{1}{N}\sum_{i=1}^{N} \ln R_i\right)$.
   - Compute a 95% confidence interval and two-sided bootstrap $p$-value using a **hierarchical bootstrap** (10,000 resamples, fixed seed `0`): resample tasks with replacement first, then resample repetitions with replacement within each sampled task. (If more than one treatment arm is ever registered, apply Holm-Bonferroni correction across non-control arms within each stratum.)
4. **Non-inferiority and fidelity metrics:**
   - Report step pass rate, full-chain resolution rate, and paired difference $\Delta \text{PassRate} = \text{PassRate}(\text{restart}) - \text{PassRate}(\text{shipped})$ with its 95% bootstrap interval against the **-5.0 pp guardrail**.
   - Report end-of-chain recall score (`0–3` facts per long chain), boundaries taken per chain, carry packet byte distribution, post-restart extra turns, and post-restart file re-discovery rate.
   - If the run stopped at `ceiling`, report all completed pairs up to the stop point alongside the fraction of planned tasks completed.

---

## 8. Deviations

None at draft creation. Any deviation from this pre-registration must be appended here with its date and commit hash before the affected runs are analyzed.
