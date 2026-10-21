# B8 Pilot — Live Run Day 1 (2026-10-21 UTC)

Harness SHA: `cddc91d474144daa6f13fa1e1b6d55fff01680fa` (main worktree HEAD).
Pre-registration: `docs/engineering/B8-PREREGISTRATION-v3.md` (binding; v1/v2 superseded).
Receipt: `evals/reports/b8_live_20261021_005434.json`.
Model: `gpt-4.1-mini`. Window cap 24,576. Step limit 35. Hand-off 8,192. Daily cap 2,200,000. Pair reserve 550,000.

## Sampled instances (seed 20261019, n=10) — matches frozen list

1. django__django-11532
2. django__django-12262
3. django__django-13279
4. django__django-14915
5. django__django-15851
6. matplotlib__matplotlib-26208
7. scikit-learn__scikit-learn-14496
8. sphinx-doc__sphinx-8035
9. sympy__sympy-12481
10. sympy__sympy-19346

All 10 parents cached with `parent_status=llm_ok` (no `llm_failed`, no `parent_failed`).
All 10 repos already extracted under `~/b8_repos`; all 10 swebench images present.

## Task × arm status (arm-major; only Arm A ran today)

| instance | arm | status | resolved | overflow | steps | billed | uncached |
|---|---|---|---|---|---|---|---|
| django__django-11532 | A | done (Submitted, agent_submission) | 0 | no | 28 | 432,116 | 24,013 |
| django__django-12262 | A | done (LimitsExceeded, empty patch) | 0 | no | 35 | 539,937 | 56,248 |
| django__django-13279 | A | done (LimitsExceeded, working_tree_git_diff) | 0 | no | 35 | 527,344 | 33,461 |
| django__django-14915 | A | done (LimitsExceeded, empty patch) | 0 | no | 35 | 473,194 | 36,276 |
| django__django-15851 | A | not_started | — | — | — | — | — |
| matplotlib__matplotlib-26208 | A | not_started | — | — | — | — | — |
| scikit-learn__scikit-learn-14496 | A | not_started | — | — | — | — | — |
| sphinx-doc__sphinx-8035 | A | not_started | — | — | — | — | — |
| sympy__sympy-12481 | A | not_started | — | — | — | — | — |
| sympy__sympy-19346 | A | not_started | — | — | — | — | — |
| all 10 | B | not_started | — | — | — | — | — |
| all 10 | C | not_started | — | — | — | — | — |

No `cap_stopped`, no `tooling_failure`, no `parent_failed` rows today.

## Ledger (UTC day 2026-10-21)

- **Total billed: 2,016,008** tokens (prompt 2,001,787 + completion 14,221; cached 1,810,304 + uncached 191,483).
- Parent localization: 43,417 tokens (10 turns). Arm A runs: 1,972,591 tokens.
- Stopped cleanly: remaining budget < 550,000 pair-reserve before the next pair (django__django-15851 A).

## Grading (Arm A)

Official report `gpt-4.1-mini.b8v3_A.json`: 4 submitted, 2 completed (non-empty patch), 2 empty-patch, **0 resolved**.
Per-instance `report.json` copied to `evals/reports/b8_pilot_reports/`:
- `b8v3_A_django__django-11532.json`
- `b8v3_A_django__django-13279.json`

## Remaining

26 task × arm runs remain (6 Arm A + 10 Arm B + 10 Arm C).

## Resume command (tomorrow, identical; `--resume` skips the 4 finished pairs)

```bash
cd /mnt/c/dev/Context-Fabric
OPENAI_API_KEY="$OPENAI_API_KEY" ~/.venvs/swe/bin/python -m evals.b8_live \
  --seed 20261019 --n 10 --model gpt-4.1-mini --repo-dir ~/b8_repos \
  --window-cap 24576 --step-limit 35 --handoff-budget 8192 \
  --daily-cap 2200000 --pair-reserve 550000 \
  --resume --rerun-tooling-failures --grade \
  >> evals/reports/b8_pilot_run.log 2>&1
```
