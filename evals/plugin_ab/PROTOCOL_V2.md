# Plugin A/B v2 — session-length-stratified chains

**Status: ready for registration.** The founder approved registering v2. The
design below, `tasks_v2.json` and `prices.json` are hashed into
`prereg_v2.sha256`; a non-pilot v2 run refuses to start unless that hash matches.
The saving is unproven until the registered run reports it.

## Why a v2

The v1 tasks are single short requests. They cannot test a mechanism that is
supposed to help a session after many turns. v2 adds scripted chains: a fixed
list of user steps sent through one `claude -p --session-id`, then `--resume`
per step, each step with its own pytest oracle on the shop fixture. A chain
reaches roughly 31–80 requests reproducibly.

## Strata and checkpoint boundaries

| Stratum | Tasks | Shape | Checkpoint arm |
| --- | --- | --- | --- |
| short | 4 (the v1 plain task ids) | one request, as in v1 | none |
| medium | 4 chains | 3–4 steps | save after step 2 |
| long | 3 chains | 8–12 steps | save after steps 4 and 8 |

The checkpoint arm saves through the same code path the
`/context-fabric:checkpoint` command uses (`cfxd checkpoint save`), with the
run's working directory and the run's `CFX_HOME`/`CFX_PORT`. The next step then
runs as a **new** session: a fresh `--session-id`, no `--resume`, and
`CFX_RESTORE_ON_STARTUP=1` so the plugin's SessionStart handler restores the
checkpoint on `source == "startup"`. A boundary is taken only when a next step
exists. Every run record reports the boundaries taken, the saved byte count and
the daemon receipt's restore outcome (counts only).

Every long chain ends with a recall probe: the final step asks for
`answers.json` with three facts fixed earlier (for example the rounding rule and
the name of an error class). A script oracle scores the three facts and the run
record keeps the score; it is not folded into the pass/fail oracle.

The chain fixture is `fixture_v2/`, a copy of `fixture/` extended with tax,
discount, coupon, refund and report steps. The original `fixture/` is untouched.
Later steps use conventions set earlier (the half-up rounding helper, the coupon
and refund error classes, the report format).

## Arms

Registered arms only:

- `off` — no plugin.
- `shipped` — plugin loaded with default settings.
- `checkpoint` — plugin loaded; checkpoint save and new-session restore at the
  boundaries above.

`diet` and `combo` were cut before registration. Diet's effect is arithmetic and
is checked with `/context`, not in this harness; combo would confound the
checkpoint mechanism. The runner rejects those labels for v2.

## Design

- **Paired.** Every task runs in every arm with the same model, prompt, fixture
  and a fresh working copy.
- **ABBA, seeded.** The v1 run order is reused (`--seed 0`); warm-up runs are
  recorded and excluded.
- **Repetitions.** 3 measured repetitions per cell, plus 1 warm-up per
  (task, arm) cell.
- **Isolation.** Per run: a temp working copy, fresh `CFX_HOME`, free
  `CFX_PORT`, fresh session UUID, and `--setting-sources project,local`. The
  same `CFX_HOME` / port / fake-HOME isolation is kept across every step of a
  chain.
- **Invocation.** Step 1 uses `--session-id <uuid>`; every later step uses
  `--resume <uuid>`, except the first step after a checkpoint boundary in the
  checkpoint arm, which gets a new `--session-id` and
  `CFX_RESTORE_ON_STARTUP=1`. Each step keeps its own oracle, timeout and usage
  record. A run result is the list of step results plus the recall score.
- **Cap.** `EVALS_MAX_RUN_USD` (default `0.60`) is a hard per-run cap. The chain
  stops after the step that crosses it and records `cap_hit`; a capped run is
  not a resolved run.

## Run count and cost

11 tasks × 3 arms × 4 runs (1 warm-up + 3 measured) = **132 confirmatory runs**.
The slice 0 harness check adds 5 runs (see below): **137 runs in total**.

Planning estimate at the pinned model: short 4 × 12 × ~$0.005 ≈ $0.24;
medium 4 × 12 × ~$0.04 ≈ $1.92; long 3 × 12 × ~$0.20 ≈ $7.20; slice 0 ≈ $0.20.
Total ≈ **$9.60**, so "about $10", with a hard ceiling of 137 × $0.60 =
**$82.20** if every run hit `EVALS_MAX_RUN_USD`.

## Exclusion rules

Exclude a run only for **infrastructure failure**: `claude` failed to launch or
timed out, the transcript is missing or unreadable, or a step oracle could not
execute. Decide exclusions blind to the arm. A run that ran but failed a step is
data, not an exclusion.

## Stop rule

Stop the experiment if two real-model runs fail infrastructure checks in a row.
No early stopping for a favourable trend.

## Claim thresholds (frozen)

| Stratum | Ratio vs off | Interval | Success | Recall |
| --- | --- | --- | --- | --- |
| short | shipped ≤ 1.05× off | — | — | — |
| medium | checkpoint ≤ 0.85 | 95% CI excludes 1.0 | at most 1 more failed run than off | drop ≤ 1 probe in 9 |
| long | checkpoint ≤ 0.80 | 95% CI excludes 1.0 | at most 1 more failed run than off out of 9 | drop ≤ 1 probe in 9 |

The ratio is the geometric mean of per-task arm/off cost ratios; the interval is
a hierarchical bootstrap (tasks, then repetitions within a task). Holm correction
is applied across the non-off arms within each stratum. Low power is admitted:
"no effect detected" is a valid result.

## Kill criteria

- Long `checkpoint` ratio > 0.90.
- Long recall drop > 2 probes in 9.
- More than 1 extra failed run than `off`.

If any kill criterion is met there is no "context saving" headline.

## Model and pricing

Pin **claude-haiku-5-5** for the measured runs. Re-price the exact same token
counts with the Opus 5.5 and Sonnet 5.5 rows of the dated price table; a claim is
made only on the re-priced result, never on the raw Haiku bill. Haiku 5.5 is 5x
cheaper under 100K prompts than above it, which would inflate compaction savings,
so the raw bill is not used for any claim. Unknown models stay unpriced and are
never guessed.

## Slice 0 (harness validation, excluded from claims)

`--slice0` runs the first long task in registered order with 1 `off` warm-up and
then ABBA of `off` and `checkpoint` (4 measured runs), model pinned to
`claude-haiku-5-5`. Results are labelled `slice0` and excluded from the
confirmatory analysis. It answers only "does the harness work end to end".

## Analysis plan

`evals/plugin_ab/analyze.py --v2` computes, per stratum and per registered arm:
the geometric mean of per-task arm/off ratios over measured, non-warm-up,
non-infrastructure runs; a 95% interval from a hierarchical bootstrap that
resamples tasks first and repetitions within each sampled task (10,000
resamples, fixed seed); a two-sided bootstrap p-value with a Holm correction
across the non-off arms in the stratum; step pass rate; recall score; extra turns
after a checkpoint; failed runs; and a frozen-threshold pass/fail column for
ratio, interval, Holm, success and recall. Slice 0 records are excluded. Costs
for claims are the same token counts re-priced at Opus 5.5, with the Sonnet 5.5
ratio reported alongside.

## Limitations

- One fixture repo.
- Haiku 5.5, not Opus; claims rest on re-pricing the same tokens.
- Scripted prompts, not free-form user sessions.
- One operating system.
- Detectable effect is about 20–25% or larger; smaller effects are not
  distinguishable.

## Deviations

None. Any deviation from this protocol is appended here with its date before the
affected run is analyzed.
