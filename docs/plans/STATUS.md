# Sprint status

Updated 2026-10-30. Active plan: `docs/plans/2026-10-30-brief-first-plan.md`;
work packages: `docs/plans/2026-10-30-brief-first-orchestration.md` (branch prefix `s11-`).

## Sprint 10 Wave 1 (done, CI green at ce67314)

| WP | State | Branch | Open items |
|---|---|---|---|
| A1 MCP isolation | merged | s10-a1 | env var ENABLE_CLAUDEAI_MCP_SERVERS unverified |
| A2 usage dedup | merged | s10-a2 | stored v2 records still per-line |
| C1 macOS CI | merged, green | s10-c1 | — |
| D1 ADRs | merged (ADR-0010, b8-parked) | s10-d1 | wheel contents decision open |
| A3 seeded recall, no_restore control, save_empty guard, D3 checkpoint billing fix | merged (1313 tests green locally) | s10-a3 | recall wording not yet pinned by any protocol |
| A4 v2 dedup re-analysis (exploratory) + deviation entry | merged | s10-a4 | inputs live outside the repo |

Old Waves 2–5 (checkpoint-vs-shipped v3) are superseded; checkpoint is secondary.

## Sprint 11 (Brief-first)

| WP | State | Branch | Blockers |
|---|---|---|---|
| B0 Brief v1 ADR | merged (ADR-0011 accepted) | s11-b0 | — |
| H1 v4 fixture + tasks | merged (click 8.5.0, 8 tasks, verify script 8/8) | s11-h1 | — |
| R1 receipts joiner | merged | s11-r1 | — |
| B1 budgets + ranking | merged | s11-b1 | B0 |
| B2 excerpts + packet v0 | merged | s11-b2 | B0 |
| B3 intent + carry-back | merged (carry-back attribution unverified) | s11-b3 | B1, B2 |
| H2 v4 harness arms | dispatched (Sonnet) | s11-h2 | H1 |
| P0 v4 pilot (gate) | blocked | — | B3, H2, R1 |

Next: assign Wave 2 (B0, H1, R1).
