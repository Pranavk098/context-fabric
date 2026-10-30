# Plugin A/B v3 — adversarial chains

**Status: EXPLORATORY, not registered.** Nothing in this file is hashed. It is
not part of `prereg_v2.sha256`, it never gates a registered run, and its results
are labelled `EXPLORATORY` in every report. `PROTOCOL_V2.md`, `tasks_v2.json`,
`prices.json`, `prereg_v2.sha256` and `fixture_v2/` stay byte-identical.

## Why an exploratory v3

The registered v2 benchmark uses one fixture repo and scripted prompts whose
workspace changes only through the agent. A restored checkpoint packet claims
only what the plugin's hooks saw; the workspace itself can change between
sessions, so an agent that trusts the packet can be misled. v3 adds a second
fixture (`fixture_v3/`, a text-processing CLI library: tokenizer, word counts,
report writer — a different domain from the shop) and four adversarial chains
where the harness changes the workspace **between** steps.

This is an exploratory study: it can find a failure mode, it cannot establish an
effect. n is 1 chain per adversarial case, 3 runs per arm, and the arms are the
registered ones only (`off`, `shipped`, `checkpoint`).

## Harness actions (`between`)

A step may carry a `between` list, applied after the step's oracle and before
the next step (a checkpoint boundary at the next step is saved first, so the
saved packet describes the workspace the agent left, not the changed one):

- `{"op": "write", "path", "content"}` — an outside edit by "someone else".
- `{"op": "revert", "path"}` — restore the file to its content before the last
  step that changed it.
- `{"op": "rename", "from", "to"}` — move a file.

A step may also declare `"killed": true`: its oracle is started and killed by a
0.05 s harness timeout, and the step is recorded with outcome `killed` — never
`failed`, never `passed`. v2 task files declare neither, so v2 runs are
unchanged. The runner refuses a v3 task file without `--exploratory`.

## Chains (6 steps each, `fixture_v3/`)

| Chain | Case | Harness change |
| --- | --- | --- |
| `control-a` | control | none |
| `control-b` | control | none |
| `adv-killed-test` | A: a test run is killed mid-run | step 5 is `killed`; the final text is scored by `oracles/claim_killed.py` |
| `adv-outside-edit` | B: a later step depends on a changed file | `textkit/limits.py` rewritten (`MAX_WORDS` 20 → 500) after step 2 |
| `adv-revert` | C: the agent's edit is reverted | `textkit/normalize.py` restored after step 4 |
| `adv-rename` | D: a file is renamed | `textkit/tokenizer.py` → `textkit/lexer.py` after step 2 |

Each adversarial chain ends with a claim step whose script oracle reads the
step's last assistant text (`.harness/agent_text.txt`) and scores it: no pass
claim plus the required fact (the killed run, the new limit, the re-applied
change, the new module). A failed claim step is counted as one misleading claim.

## Hypotheses

- **H1:** a checkpoint restore does not increase misleading claims versus `off`.
- **H2:** stale marks are noticed in at least 3 of the 4 adversarial cases (a
  case is noticed when the `checkpoint` arm's claim oracle passes).

Both are exploratory. H1 is checked descriptively (claim counts per arm); no
interval, no significance test.

## What would count against the checkpoint

Any adversarial case where `checkpoint` fails the oracle and `off` passes. One
such case is enough to say the checkpoint arm misled the agent in this study; it
is not a claim about frequency.

## Analysis

`evals/plugin_ab/analyze.py --v3` reports, per chain and arm: the run outcome
(`pass`, `fail`, `killed`), the counts behind it, and the misleading-claim count.
The output starts with `EXPLORATORY: n is small; no confirmatory claim`. There
are no bootstrap intervals and no thresholds.

## Cost estimate (arithmetic only; nothing here is run by this branch)

- Registered-shaped run: 6 chains × 3 arms × 4 runs (1 warm-up + 3 measured) =
  **72 runs**. At the v2 planning rate for a 6-step chain, ~$0.03/run at Haiku
  5.5: 72 × $0.03 ≈ **$2.16**, with a hard ceiling of 72 × $0.60 = **$43.20**.
- `--smoke` (fake claude only): 1 chain × 2 arms = **2 runs**, $0.00.
- Sonnet direction check (direction only): 4 adversarial chains × 2 arms
  (`off`, `checkpoint`) × 1 run = **8 runs**. Sonnet 5.5 is 20× Haiku 5.5 on
  input ($2.00 vs $0.10/M tokens) and 20× on output ($10.00 vs $0.50/M), so
  ~$0.03 × 20 = $0.60/run ≈ **$4.80**, i.e. the per-run cap. Reported as a
  direction check on the same four cases, never as an effect size.

## Limitations

- One chain per adversarial case; 3 measured runs per arm.
- A scripted claim step, not a free-form user session; the oracle judges text,
  not truth.
- One fixture, one OS, Haiku 5.5 for measured runs.
- The harness changes are synthetic; a real outside edit is not this clean.
