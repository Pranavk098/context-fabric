# v2 registered runs, recomputed with dedup (EXPLORATORY)

Not a registered result. The registered off-based ratios are void; see
`docs/deviations/2026-10-30-v2-mcp-race-and-double-count.md`.

## Files

- `recompute.py`: reads the v2 run records and the raw Claude Code transcripts, counts
  usage with the analyzer's own `evals.plugin_ab.usage.scan_transcript` (once per
  `message.id`), and computes ratios with `evals.plugin_ab.analyze.analyze_v2`. No model
  calls.
- `results.md`: the output tables.
- `results.json`: the same plus per-run weighted tokens, session counts and
  `deferred_tools_delta` counts.

## Inputs

- Run records: `C:/dev/cf-bench/evals/reports/v2_registered/v2-registered-{short,medium,long}.json`
  (132 runs, warm-ups included; produced by the frozen worktree at cf-bench `21445e5`).
- Transcripts: `~/.claude/projects/*plugin-ab-*/<session_id>.jsonl`. A run's project
  directory (one per task/arm/rep work dir) holds every session the run wrote. All 132
  record session ids were found.

Neither input is in this repo, so the script cannot be re-run from a clean clone.

## Reproduce

```
uv run python evals/reports/v2_registered_dedup/recompute.py \
    --records C:/dev/cf-bench/evals/reports/v2_registered \
    --projects ~/.claude/projects
```

It overwrites `results.md` and `results.json` next to itself.

## Columns

- `registered`: the records as written. This matches cf-bench's `analysis_v2.md`.
- `dedup, final session`: once-per-id counting over the record's final `session_id`
  only. That is the runner's scope, so this column isolates the double-count fix.
- `dedup, all sessions`: once-per-id counting over every session of the run. Checkpoint
  runs open a fresh session at each boundary, and the runner's step deltas kept only the
  last one (see the deviation entry). Off and shipped runs have one session each, so the
  two dedup columns are the same for them.

Costs are repriced at the single-tier Opus 5.5 table. The script asserts that table is
single-tier, so pricing the per-run totals equals the sum over turns. The Sonnet 5.5
ratio is in `results.json`.

The checkpoint vs shipped table relabels shipped as the reference arm and drops off, then
runs the same `analyze_v2`. Every off-based column is still confounded by the MCP race,
dedup included.
