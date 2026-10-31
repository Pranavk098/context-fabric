# ADR-0012 — Retire fold; supersede checkpoint-restart with carry v2

- **Status:** Accepted 2026-10-31 (coordinator, under the founder's sprint authority; founder may amend)
- **Date:** 2026-10-31
- **Work package:** R-8 in `docs/plans/2026-10-31-feature-rebuild-plan.md`; binds R-4
- **Evidence:** `docs/paper/EVIDENCE-DUMP-2026-10-31.md` (rows M1–M4, in §2; fold and checkpoint rows in §3),
  `README.md` (evidence table, folding rows), `docs/decisions/ADR-0007-fold-expand-via-saved-file.md`,
  `docs/FIDELITY-METRICS.md`, `docs/deviations/2026-10-30-v2-mcp-race-and-double-count.md`,
  `docs/plans/2026-10-31-feature-rebuild-plan.md` (verdict table and R-4), `cfxd/carry.py`,
  `hooks/hooks.json`, `cfxd/config.py` (`fold: False`, `carry: True`)

## Context

Two features were built on plausible token-saving stories and then measured. Both lost.

**Fold** replaces very large `Read`, `Bash`, `Grep` and `WebFetch` output with a short excerpt and a pointer to a
saved copy (ADR-0007). On tasks that read whole files it cost **+101%** tokens: the model re-read the saved copy
instead of using the excerpt. On long command output there was no measurable saving, because Claude Code already
shortens very large output. It is off by default (`fold: False`) and its stated goal — shrinking big tool
outputs — is now served by **bound**, which is measured separately (R-6).

**Checkpoint** saves the session's working state, asks the user to `/clear`, and restores the state into a fresh
session. That is a restart: it re-pays the prefix and loses the prompt cache. The v2 registered run, recounted
with usage deduplicated across every session, puts checkpoint vs shipped at **1.251×** on long sessions
(1.182–1.309), **1.374×** on medium, and 0.997× on short — i.e. *more* expensive, not less. The earlier "0.44"
signal came from counting only the last session of a run; the deviation doc voids it. Fidelity also shows the
loss the restart is meant to fix: compaction drops **460 of 614** edited files and names a median of only 21% of
held files (M1, M2), yet agents rarely re-read what was dropped (M5).

## Decision

1. **Fold is retired.** Keep the code and its tests, off by default; add no features and make no public saving
   claim. `fold` remains a valid flag so existing installs do not break, but new work on large tool output goes to
   bound. ADR-0007 stays as history and is not amended.
2. **Checkpoint-restart is superseded by compaction carry v2.** Keep `cfxd checkpoint` and the `/checkpoint`
   workflow working for users who rely on them, but stop developing or recommending the restart path. The
   replacement improves what survives `/compact` through the existing `PreCompact` / `SessionStart(compact)` path
   in `cfxd/carry.py`; it needs no restart, no new session and no re-paid prefix.
3. **Carry v2 is defined by R-4, not by this ADR.** In one line: order edited files first with their line ranges,
   then the last test result, open todos and the user's constraints, cap the block at 4 KB, and keep the existing
   safety rules (content-free facts, no tool output). R-4 owns the implementation, tests and the fidelity scorer
   that compares v1 and v2 restore rates; this ADR only fixes the direction.

**Amends:** nothing in code or config. This records the verdicts the feature rebuild plan already acts on.

## Consequences

- Defaults are unchanged: `fold: False`, `carry: True`. No migration is needed.
- Fold's `obs/` store, `folds.jsonl` and the `cfxd fold` surface stay in place so existing saved outputs still
  expand; they get no new work.
- Checkpoint's save/restore, `cfxd checkpoint show|clear|import` and the 60-minute restore window stay documented
  as experimental. They are no longer the recommended answer to compaction loss.
- Carry v2 can only be claimed after R-4's scorer shows it restores at least twice as many dropped edited files as
  v1; until then the plan treats it as unproven and no paid confirmation is scheduled.
- The two negative results stay in the paper material as evidence, not as shipped claims.

## Open for the founder

- Remove fold from the wheel and the `cfxd fold` command surface, or keep it shipped and off indefinitely?
- Retire `/checkpoint` from the tester docs once carry v2 passes its offline check, or keep both workflows
  documented side by side?
