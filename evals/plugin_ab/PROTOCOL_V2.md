# DRAFT: plugin A/B v2 — session-length-stratified chains

**Status: DRAFT, not registered.** This file is not part of `prereg.sha256`, and
`PROTOCOL.md` / `tasks.json` are unchanged. Nothing here supports a confirmatory
claim until it is registered and the arms it names are implemented. The saving
is unproven.

## Why a v2

The v1 tasks are single short requests. They cannot test a mechanism that is
supposed to help a session after many turns. v2 adds scripted chains: a fixed
list of user steps sent through one `claude -p --session-id`, then `--resume`
per step, each step with its own pytest oracle on the shop fixture. A chain
reaches roughly 31–80 requests reproducibly.

## Strata

| Stratum | Tasks | Shape |
| --- | --- | --- |
| short | 4 (the v1 plain task ids) | one request, as in v1 |
| medium | 4 chains | 3–4 steps |
| long | 3 chains | 8–12 steps |

Every long chain ends with a recall probe: the final step asks for
`answers.json` with three facts fixed earlier (for example the rounding rule and
the name of an error class). A script oracle scores the three facts and the run
record keeps the score; it is not folded into the pass/fail oracle.

The chain fixture is `fixture_v2/`, a copy of `fixture/` extended with tax,
discount, coupon, refund and report steps. The original `fixture/` is untouched.
Later steps use conventions set earlier (the half-up rounding helper, the coupon
and refund error classes, the report format).

## Design

- **Paired.** Every task runs in every arm with the same model, prompt, fixture
  and a fresh working copy.
- **ABBA, seeded.** The v1 run order is reused (`--seed 0`); warm-up runs are
  recorded and excluded.
- **Repetitions.** 3 measured repetitions per cell, plus 1 warm-up per
  (task, arm) cell.
- **Arms.** `off` (no plugin), `shipped` (plugin as shipped), `diet`,
  `checkpoint`, `combo`. The analyzer groups by these labels. The runner
  currently implements `off` and loads the plugin for every other label; `diet`,
  `checkpoint` and `combo` are reserved labels and need their own plugin
  variants before a run can claim on them.
- **Isolation.** Per run: a temp working copy, fresh `CFX_HOME`, free
  `CFX_PORT`, fresh session UUID, and `--setting-sources project,local`. The
  same `CFX_HOME` / port / fake-HOME isolation is kept across every step of a
  chain.
- **Invocation.** Step 1 uses `--session-id <uuid>`; every later step uses
  `--resume <uuid>`. Each step keeps its own oracle, timeout and usage record.
  A run result is the list of step results plus the recall score.
- **Cap.** `EVALS_MAX_RUN_USD` (default `0.60`) is a hard per-run cap. The chain
  stops after the step that crosses it and records `cap_hit`; a capped run is
  not a resolved run.

## Exclusion rules

Exclude a run only for **infrastructure failure**: `claude` failed to launch or
timed out, the transcript is missing or unreadable, or a step oracle could not
execute. Decide exclusions blind to the arm. A run that ran but failed a step is
data, not an exclusion.

## Stop rule

Stop the experiment if two real-model runs fail infrastructure checks in a row.
No early stopping for a favourable trend.

## Claim thresholds

| Stratum | Ratio vs off | Interval | Success | Recall |
| --- | --- | --- | --- | --- |
| short | shipped ≤ 1.05× off | — | — | — |
| medium | ≤ 0.85 | 95% CI excludes 1.0 | — | — |
| long | ≤ 0.80 | 95% CI excludes 1.0 | at most 1 more failed run than off out of 9 | drop ≤ 1 probe in 9 |

The ratio is the geometric mean of per-task arm/off cost ratios; the interval is
a hierarchical bootstrap (tasks, then repetitions within a task). Low power is
admitted: "no effect detected" is a valid result.

## Kill criteria

- Long `checkpoint` ratio > 0.90.
- Long recall drop > 2 probes in 9.
- More than 1 extra failed run than `off`.

## Model and pricing

Pin **claude-haiku-5-5** for the measured runs. Re-price the exact same token
counts with the Opus 5.5 and Sonnet 5.5 rows of the dated price table; a claim is
made only on the re-priced result, never on the raw Haiku bill. Unknown models
stay unpriced and are never guessed.

## Cost and detectable effect

About 144 runs, about **$10** at the pinned model. The design can detect a
**20–25%** or larger cost effect; smaller effects are not distinguishable.

## Always reported

Per stratum: the ratio per arm vs off with its interval, step pass rate, recall
score, extra turns after a checkpoint, and a pass/fail column for every claim
threshold. Also: losses (arm costs more), plugin overhead, resolve rate per arm,
infra exclusions, and raw per-run JSON with counts and ids only.
