# v2 registered run: MCP race and usage double-count (found 2026-10-29, recorded 2026-10-30)

- **Protocol:** `evals/plugin_ab/PROTOCOL_V2.md` (prereg sha256 `7ee3a3cc…`), run
  `v2-registered-{long,medium,short}` from the frozen cf-bench worktree at `21445e5`, Haiku 5.5,
  3 reps plus 1 warm-up, 132 runs.
- **Decision:** every registered **off-based ratio is VOID**. The recomputed numbers below are
  **EXPLORATORY**. The checkpoint vs shipped ratio is reported but **not claimed**. The
  protocol, the registered report and the README are unchanged.

## D1: MCP connector race in the harness (affects off)

Each step is a fresh `claude -p --resume`. The account's connector tools were not blocked by
`--setting-sources project,local`. In off, the first turn starts before the connectors finish
connecting, so on every resume Claude Code announces the tool list again. In the paired
long-checkout rep 0 transcripts, off has 27 `deferred_tools_delta` attachments and shipped has 1.
Shipped's SessionStart hook delays the first turn long enough that the list is announced once.
Off therefore paid a cache rewrite on most steps that the plugin did not cause.

`deferred_tools_delta` attachments per measured run, counted over all of a run's sessions
(`evals/reports/v2_registered_dedup/results.md`):

| stratum | off | shipped | checkpoint |
| --- | --- | --- | --- |
| long | 27, 24, 24, 14, 19, 16, 20, 20, 20 | 1, 1, 1, 1, 1, 1, 1, 1, 3 | 3, 8, 3, 2, 2, 2, 3, 3, 3 |
| medium | 7, 7, 6, 8, 8, 8, 8, 8, 8, 9, 6, 8 | all 1 (12 runs) | 2, 3, 2, 2, 2, 2, 2, 2, 2, 3, 2, 2 |
| short | 2, 2, 2, 2, 3, 3, 2, 3, 1, 2, 3, 3 | all 1 (12 runs) | all 1 (12 runs) |

Deduplicating usage does not remove this confound. Every off-based ratio, the dedup ones
included, compares against an inflated off arm.

## D2: per-line usage double count in the analyzer (affects every arm)

Claude Code writes one transcript line per content block and repeats the message's `usage` on
each line. The runner summed lines, not messages. The inflation differs by arm, so ratios move.
For long-checkout rep 0, once-per-`message.id` counting gives these weighted tokens:

| run | registered | dedup |
| --- | --- | --- |
| off | 1,644,463.95 | 582,862.6 |
| shipped | 812,517.3 | 331,585.1 |

The fix is `cfxd/usage_dedupe.py`, used by `evals/plugin_ab/usage.py` on main.

## D3: checkpoint runs were billed for their last session only (affects checkpoint; found during this re-analysis)

At each registered boundary, the checkpoint arm starts a fresh session id. The runner did not
reset its running totals when the session changed. Each step's usage is the delta between
cumulative scans, so the first step after a boundary has a negative delta, and the steps sum to
the final session's total. Every session before the last was dropped from the checkpoint cost.
For example, long-checkout warm-up `discount-bulk` has a parent `cache_read_input_tokens` delta
of -1,133,282 in `v2-registered-long.json`. Long checkpoint runs have 2 or 3 sessions and medium
checkpoint runs have 2. Off and shipped runs have 1 session each.

This alone makes the registered checkpoint ratios unusable, the off-based ones and the
checkpoint vs shipped ratio alike. The `dedup, all sessions` column counts every session.

## Old vs dedup ratios per stratum (EXPLORATORY)

Geometric mean over tasks of the arm's mean cost divided by the reference arm's mean cost,
priced at Opus 5.5, with the registered hierarchical bootstrap 95% CI. Produced by
`evals/reports/v2_registered_dedup/recompute.py`, which uses the analyzer's own
`scan_transcript` and `analyze_v2`. Transcripts were found for all 132 runs.

**Arm vs off: void because of D1, in every column.**

| stratum | arm | registered | dedup, final session | dedup, all sessions |
| --- | --- | --- | --- | --- |
| short | shipped | 0.877 (0.713-1.099) | 1.002 (0.960-1.038) | 1.002 (0.960-1.038) |
| short | checkpoint | 0.842 (0.666-1.056) | 0.999 (0.962-1.043) | 0.999 (0.962-1.043) |
| medium | shipped | 0.685 (0.601-0.794) | 0.761 (0.712-0.832) | 0.761 (0.712-0.832) |
| medium | checkpoint | 0.570 (0.486-0.667) | 0.500 (0.433-0.557) | 1.046 (0.984-1.106) |
| long | shipped | 0.545 (0.488-0.612) | 0.624 (0.555-0.684) | 0.624 (0.555-0.684) |
| long | checkpoint | 0.240 (0.196-0.322) | 0.299 (0.245-0.411) | 0.781 (0.714-0.848) |

**Checkpoint vs shipped: reported, not claimed.** Both arms load the plugin, so both
churned far less than off (table above). One long checkpoint run still had 8 announcements
over its sessions. This is still post hoc and was not the
registered primary comparison.

| stratum | registered | dedup, final session | dedup, all sessions |
| --- | --- | --- | --- |
| short | 0.960 (0.761-1.127) | 0.997 (0.952-1.050) | 0.997 (0.952-1.050) |
| medium | 0.832 (0.700-1.002) | 0.656 (0.536-0.750) | 1.374 (1.286-1.452) |
| long | 0.441 (0.351-0.568) | 0.479 (0.381-0.628) | 1.251 (1.182-1.309) |

`dedup, all sessions` is the only column that counts every session once per message. In that
column, checkpoint costs more than shipped in medium and long.

## Follow-up

- MCP isolation and a one-`deferred_tools_delta` assert, so a run with churn becomes an infra
  failure (WP A1).
- Once-per-`message.id` counting with a regression test (WP A2, merged).
- The runner's chain loop must reset or carry its running totals when a checkpoint starts a
  new session (D3). This is not fixed by A1 or A2.
- v3 is registered with checkpoint vs shipped as the primary comparison and is run only after
  the off ≈ shipped gate probe (WP A5) passes.
