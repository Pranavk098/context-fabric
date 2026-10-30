# v4 pilot runbook (P0, unregistered, founder runs it)

Purpose: size budgets and catch harness bugs before H3 freezes the protocol. Not evidence for any claim.

## Run

From `C:\dev\cf-bench` (benchmarks only), after fetching main:

```
git fetch origin && git checkout --detach origin/main
uv sync
export EVALS_MAX_RUN_USD=1.0      # per-run cap; 10 runs => at most about $10
uv run python -m evals.plugin_ab.runner --v4 --pilot \
  --tasks evals/plugin_ab/tasks_v4.json \
  --task-ids v4-command-aliases,v4-option-sections \
  --arms none,brief_v0,brief_v1,fork,control \
  --repetitions 1 --seed 1 \
  --run-id v4-pilot-1 --report-dir evals/reports/v4_pilot
```

2 tasks x 5 arms x 1 rep = 10 runs (the pilot cap). Parent Sonnet, children Haiku. Use a separate
`--report-dir`; the v1/v2 analyzers would pick up v4 records otherwise. Before the run, turn Brief
on in your own profile is NOT needed: the harness writes the per-arm config itself.

Analyze (fake or real records): `uv run python -m evals.plugin_ab.analyze --v4 --report-dir evals/reports/v4_pilot` (check `--help`).

## Gate checks (the coordinator reads the transcripts)

1. Spawn rate: children spawn in every arm (`no_spawn` is data, but a pilot with no spawns is a prompt bug).
2. `control` fails the planted-fact tests (the quality check is valid).
3. Zero `mcp_churn` infra failures.
4. Real `brief_v1` packet sizes against the 6,000-byte Haiku budget, and the fallback rate (`v1-error`, `v1-deadline`).
5. Fork arm: `child_parent_msg_overlap > 0` and `child_models` (fork children may inherit Sonnet, which confounds cost).
6. Children actually run on Haiku in the other arms.
7. Per-run cost against the cap; read at least one transcript per arm.

Output goes to `docs/plans/v4-pilot-notes.md`.
