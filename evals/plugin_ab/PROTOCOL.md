# Pre-registration: Context Fabric plugin paired benchmark (on vs off)

- **Registered:** 2026-10-24
- **Hash:** `prereg.sha256` = SHA-256 of this file + `tasks.json` + `prices.json`,
  in that byte order. The runner refuses to start a non-pilot run unless the
  committed hash matches, so nothing below can change after the founder commits
  the hash.
- **Scope:** measures the plugin, not the model. It must never call a real model
  outside the confirmatory run, and it spends no API budget in the harness tests.

## Hypotheses

- **H1** — Brief reduces cache-weighted cost on **sub-agent tasks** (tasks whose
  `kind` is `subagent`). Only `on` runs where the brief actually fired count
  toward H1 (per-protocol).
- **H2** — Carry reduces cache-weighted cost on **compact tasks** (tasks whose
  `kind` is `compact`). Only `on` runs where carry actually rendered count
  toward H2 (per-protocol).
- **H0** — No effect on plain tasks or anywhere the mechanism did not fire.

## Primary metric

API-equivalent **USD per resolved task**, using the dated price table in
`prices.json` (`as_of: 2026-10-24`), cache-weighted, sub-agents included:

    sum over requests of
        input_tokens * input
      + cache_creation_tokens_5m * cache_write_5m
      + cache_creation_tokens_1h * cache_write_1h
      + cache_read_tokens * cache_read
      + output_tokens * output
    (each per-MTok, / 1e6), divided by the number of resolved tasks.

Claude Code transcripts merge 5-minute and 1-hour cache-creation into one
`cache_creation_input_tokens` field; the harness prices that merged value at the
5-minute write rate unless a transcript carries explicit
`cache_creation_input_tokens_5m` / `_1h` fields. Unknown models are reported as
unpriced and never guessed. Secondary metrics: turns, weighted tokens
(`input + 1.25*cache_creation + 0.1*cache_read + 5*output`), wall time.

## Design

- **Paired.** Every task runs in both arms with the same model (`haiku`), prompt,
  fixture commit and fresh working copy.
- **ABBA, seeded.** Run order is randomized with a fixed seed (`--seed 0`); per
  task the arm order is flipped by a coin and measured repetitions alternate
  forward/reverse, so time and price drift hit both arms symmetrically.
- **Repetitions.** 3 measured repetitions per cell, plus 1 warm-up run per
  (task, arm) cell. Warm-up results are recorded and flagged but excluded from
  every analysis.
- **Arms.** `off` (no plugin) and `on` (`--plugin-dir <repo root>`, brief and
  carry on, fold off, as shipped). Optional third arm `on_fold` (fold and
  fold_read on via the isolated config) behind `--with-fold`.
- **Isolation.** Each run gets its own temp working copy, a fresh `CFX_HOME`, a
  free `CFX_PORT`, a fresh `CLAUDE_CONFIG_DIR`, and a fresh `--session-id`.
- **Invocation.** `claude -p … --model haiku --output-format json --max-turns 25
  --max-budget-usd 0.50 --permission-mode acceptEdits --allowedTools
  "Read,Edit,Write,Grep,Glob,Agent,Bash(python -m pytest:*)"`. Compact tasks run
  as three calls on one session (read+plan, `/compact`, implement) via
  `--session-id` then `--resume`.

## Exclusion rules (decided before looking at the arm)

- Exclude a run only for **infrastructure failure**: the `claude` process failed
  to launch or timed out, the transcript is missing or unreadable, or the oracle
  could not execute. Decide exclusions blind to the arm. A run that ran but
  failed to resolve the task is **data, not an exclusion**.

## Stop rule

- Stop the experiment if a real-model run fails infrastructure checks twice in a
  row, or if any single run exceeds the `--max-budget-usd 0.50` cap. No early
  stopping for a favourable trend.

## Claim threshold

- Claim a saving only if, on the protocol-relevant analysis, the geometric-mean
  on/off cost ratio is **at least 15% cheaper** (ratio ≤ 0.85), the **95%
  bootstrap interval excludes 1.0** (no effect), **and** the resolve rate is
  **non-inferior**: the `on` arm fails to resolve at most **1 of the 8 tasks**
  more than the `off` arm. Low power is admitted; "no effect detected" is a
  valid result, "a saving" without this is not.

## Always reported

- A **losses** table (tasks where `on` cost more).
- Plugin **overhead** (hook call count and latency total from `events.jsonl`).
- **Per-protocol** (mechanism fired) next to **intention-to-treat** (all `on`
  runs).
- Resolve rate per arm and the on/off disagreement counts.
- Raw per-run JSON with per-request usage (no prompt or transcript text).
