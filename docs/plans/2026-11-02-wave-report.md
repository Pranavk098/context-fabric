# Plugin waves report (2026-11-02)

Base: `main` at `24092b4` (plan) → `w-integration`. Decisions commit: `b6f1c26`. Orchestrator: Claude Sonnet 5.5.
Builders: OpenCode (deepseek-v4.1-flash) and Antigravity (Gemini). G-FULL on the final state:
**1,973 passed, 20 skipped**; ruff, format, mypy and docs-numbers clean.

## Work packages

| WP | Agent | Status | Review notes |
|---|---|---|---|
| W1-01 share v2 serializer | OpenCode | merged (+W1-01b) | Review found free text could pass `plugin_version`/`metric` and NaN/negative counts were accepted; fixed and tested |
| W1-02 security hardening | OpenCode | merged (+W1-02b) | Real-state-dir check found `welcomed` undeclared (would warn every start); declared, plus a static scan that forces new write targets to be declared. SECURITY.md text **held** |
| W1-03 limit metric | OpenCode | merged | No official token-denominated limit exists for any tier, so `limit_share()` is `None` (D-3 fallback); an unverifiable comment removed |
| W1-04 repeat-share (M1, M2) | OpenCode | merged | definition matches P4; both denominators printed |
| W1-05 restart break-even (M3) | OpenCode | merged (+W1-05b) | Review found the restart branch never paid each turn's new tokens, biasing toward "restart wins"; fixed with a falsifying test |
| W1-06 coach control + sweep | OpenCode | merged | install-level hash arm; counts/dollars only; `scripts/coach_sweep.py` ships, setting not yet frozen |
| W1-07 defaults + goldens | OpenCode | merged | goldens hold a `{VERSION}` placeholder so a bump cannot break them |
| W1-08 diet observed before/after | OpenCode | merged | revert-first tests found that an interrupted apply could not be undone; fixed |
| W1-09 CLI log formats | Gemini | merged | Codex, Gemini CLI, OpenCode all "reader feasible" per the doc |
| W2-01 CLI wiring | OpenCode | merged | week/handoff-stats lead with limit-weighted tokens; `--include-name-digests` warns; every command has `--version` |
| W2-02 deep doctor + hook log | OpenCode | merged | wired into `cfxd doctor` by W2-06 |
| W2-03 guides + copy audit | Gemini | merged | D-1 disclosure verbatim; two separate consents; banned-phrase test |
| W2-04 intake (+W2-04b) | OpenCode | merged | Review found one developer submitting twice counted as two toward the floor of 3; fixed |
| W2-05 working-set tracker (+W2-05b) | OpenCode | merged | Review found a child's free-text summary entering `tried_failed` (R10); replaced with a fixed label |
| W2-06 doctor wiring | OpenCode | merged | paste-safe `~` paths |
| W3-01 release prep | OpenCode | merged | version 0.1.1a2; orchestrator removed stale checkpoint wording |
| W3-02 cold-install checklist | Gemini | merged | needs the founder |
| W3-03 backward dedupe | — | **parked** | M1 = 0.1% of new tokens, below the 5% floor |
| W3-04 Codex reader (beta) | OpenCode | merged | verified on 12 real Codex sessions (needs the optional zstd decoder); Claude path untouched |
| W3-05 pre-registration | Gemini | merged, **NOT REGISTERED** | needs a P7 harness before the paid probe can run |

## P4 verdicts (`2026-11-02-p4-results.md`)

M1 repeat-share 0.1% → demote. M3 restart break-even 40.9% of 44 long sessions → clears D-2 (modelled, one machine, complete-packet assumption).

## Founder actions needed

1. **Approve or edit the SECURITY.md draft** (`docs/plans/SECURITY-DRAFT-2026-11-02.md`, on the `w1-sec-draft` worktree branch; not merged anywhere).
2. **Decide the coach default.** D-1 puts testers in a coach study, but `coach` itself is still off, so no data arrives unless testers turn it on.
3. Tester hold, cold-install rehearsal, and any paid probe.
