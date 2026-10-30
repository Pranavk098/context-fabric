# B8 is parked

- **Status:** Proposed (founder confirms in the PR)
- **Date:** 2026-10-29
- **Work package:** D1 in `docs/plans/2026-10-29-orchestration-plan.md`; item 5 in `docs/plans/2026-10-29-ten-day-plan.md`
- **Evidence:** `docs/engineering/B8-PREREGISTRATION-v3.md`, `docs/engineering/B8-DEVIATIONS.md`,
  `evals/reports/b8_batch_report_20261021.md`, `docs/engineering/RELEASE-v0.2.md`, `evals/b8_live.py`,
  `docs/adr/0001-compiler-vs-plugin.md`

## Context

B8 asks whether a sub-agent on a small window (24,576 input tokens) resolves more SWE-bench Verified tasks, with fewer
overflows and fewer tokens, when the parent hands off a Context Fabric packet instead of raw files (v3 §1). It is a
pre-registered pilot: 10 tasks, three arms (A naive, B compiled packet, C hand-off plus observation folding),
`gpt-4.1-mini`, official Docker grading, a daily token cap inside the free allowance (v3 §2, §3, §6).

What was built and what happened:
- Harness `evals/b8_live.py`, ledger and window cap in `sidecar/b8.py`, aggregator `evals/b8_aggregate.py`, WSL2
  setup scripts `scripts/wsl_b8_setup.sh` and `wsl_b8_extract.sh`, tests `tests/test_b8.py`.
- v1 and v2 were voided before any arm comparison was seen (deviations D1 to D3 in `B8-DEVIATIONS.md`).
- v3 was frozen on 2026-10-20. Day 1 (2026-10-21) cached all 10 parents and finished 4 of 30 task × arm runs, all
  arm A, none resolved; 26 runs remain (`b8_batch_report_20261021.md`). The same report shows the daily cap fitting
  only 4 arm runs that day.
- The project owner then ended the pilot. There is no result document and no claim depends on it
  (`RELEASE-v0.2.md`, status note dated 2026-10-24).

Why it is parked rather than resumed now:
- It is not on the sprint's critical path; the ten-day plan lists B8 under "Explicitly not before testers" (§2).
- Arm B tests `kernel.router.compiler.compile_packet` (`evals/b8_live.py`), which ADR 0001 makes internal. A B8
  result would speak about the compiler, not the plugin.

## Decision

1. B8 v3 stays parked. Nothing is deleted. The harness, receipts and reports stay where they are.
2. The 4 finished arm A runs are not a result and are never quoted as one (v3 §5 verdicts compare arms; no B or C
   run exists).
3. B8 v3's text is not edited. Any change of sample, arms, model or settings needs a new, separately dated
   pre-registration (v3 §6).

## Re-entry criteria (all must hold)

1. **A reason to need the answer.** Either the founder wants a sub-agent / SWE-bench claim, or wants to cut the
   sidecar v0.2 release, whose Phase B requires a completed B8 v3 pilot (`RELEASE-v0.2.md`, Phase B checklist).
2. **The plugin sprint has its result.** The registered v3 plugin benchmark has been analysed (WP P2), so B8 does not
   compete with the critical path.
3. **The v3 configuration is still runnable as registered.** The pinned dataset revision, `gpt-4.1-mini`, the free
   daily allowance that the 2,200,000-token cap sits inside (v3 §6), and the WSL2 + Docker grader environment are all
   still available. If any is not, B8 restarts under a new pre-registration rather than resuming v3.
4. **The harness is unchanged since the run, or re-checked.** Since harness commit `cddc91d` (the day-1 report), the
   only change under `kernel/`, `sidecar/`, `protocol/`, `adapters/` and the B8 eval scripts is an addition to
   `sidecar/mcp.py`; `pyproject.toml` and `uv.lock` changed too (checked with `git diff --stat` on 2026-10-29). Before
   resuming, run from the run commit or show the diff does not touch the B8 import path.
5. **The floor risk is accepted in writing.** Arm A resolved 0 of 4 so far, and v3 §7 already warns every arm may
   resolve few tasks. Resuming means accepting that an INCONCLUSIVE verdict is likely, before arms B and C run.

## Consequences

- The sidecar's `--obs-fold` path (arm C) and the compiled packet (arm B) keep no live task-success test.
- `CHANGELOG.md` still says the B8 pilot "is in progress"; that line is now out of date and is left for a docs WP.
- At the day-1 pace (4 arm runs, 26 remaining; `b8_batch_report_20261021.md`), resuming takes several days.
