# Delivery status and the tester-build sprint (2026-10-31 → 2026-11-01)

Part 1 is what's built and whether it works. Part 2 is what's left. Part 3 is the Orca
plan to deliver it in two days on cheap builders. External audit questions are in
`docs/audit/2026-10-31-muse-audit-packet.md`.

## Part 1 · What's built, and is it doing its job?

Legend: ✅ works as intended · 🟡 works, effect unproven or below bar · ❌ disproven or
broken · ⏸ parked

| Area | Feature | State | Verdict | Keep for testers? |
|---|---|---|---|---|
| Measure | `audit`, `diet` report, `waste`, `week --share`, `ledger`, `stats` | main | ✅ | yes |
| Measure | `fidelity` (what /compact forgets), `refetch` | main | ✅ one machine | yes; lead feature |
| Measure | `routing` by child model (R-9) | s12-integration | ✅ price only | yes |
| Measure | MCP churn rule + doctor warning (R-7) | s12-integration | 🟡 dollar split unchecked | yes, after T-6 |
| Act | Brief v1 + ordering fix + policy (R-2) | main + s12 | 🟡 covers 92% of re-reads, precision 11.8%, no paid test | yes, opt-in |
| Act | `cfxd brief` receipts view | main | ✅ | yes |
| Act | Carry v1 (post-compact facts) | main | ✅ functions; effect unmeasured | yes |
| Act | Carry v2 (R-4) | s12-integration | 🟡 1.4× v1 (bar 2×); **live without a flag** | behind flag (T-1) |
| Act | Diet `--apply/--undo` | main | ✅ deterministic; dollar estimate pending (R-5) | yes |
| Act | Coach | main, off | 🟡 precision 100%, recall 35% | after tuning (T-5) |
| Act | Bound, fold, checkpoint restart | — | ❌ retired (ADR-0012) | no |
| Act | Carry-back | main, off | ❌ broken for async children; design doc only | no |
| Cross-agent | packet v0, handoff, AGENTS.md install, demo | main | ✅ pointers only | yes, as a demo |
| Bench | Harness: MCP isolation, dedup, planted facts, v4 arms | main | ✅ pilot clean | internal |
| Bench | v4 registered run, Gate 2 | — | ⏸ on hold (cost, usage limits) | — |
| Ops | macOS CI, install + release scripts, tester guide | main | ✅ CI; guide commands checked | yes |
| Ops | B8 | — | ⏸ parked | — |

**Branch state:** `s12-integration` (local, unpushed) holds R-1, R-2, R-3 doc, R-4, R-6,
R-7, R-8 and R-9. R-5 (diet delta) is still being fixed. Nothing from Sprint 12 is on
`main` yet.

## Part 2 · What's still to deliver before testers

### Must have (tester build is wrong or unsafe without them)
| ID | Item | Why |
|---|---|---|
| T-0 | Finish R-5, full checks on `s12-integration`, merge to `main` | Sprint 12 isn't shipped until then |
| T-1 | `carry_v2` flag (default off) | v2 missed its bar and changes live compactions |
| T-2 | SECURITY.md refresh | stale carry size (3,000 → 4 KB); Brief v1 excerpts are a new injection surface; carry v2 adds constraints/ranges |
| T-3 | README evidence refresh | checkpoint row still says "paired test registered"; add disproven/retired rows and Gate 1, all marked exploratory; docs-numbers check must pass |
| T-4 | Feedback forms | `docs/FEEDBACK-TEMPLATE.md` and the old issue forms don't mention Brief; retire or redirect them |

### Should have (makes testers' feedback useful)
| ID | Item | Why |
|---|---|---|
| T-5 | Coach threshold sweep + new defaults | recall 35% → target ≥ 60% at ≥ 80% precision, offline |
| T-6 | MCP churn verification | split the 4.4M tokens into cache writes/reads and dollars; separate the expected first announcement from the churn; fix the doctor text |
| T-7 | Brief precision pass | raise R-1 precision from 11.8% without losing coverage (tighter top-k by prompt overlap, fewer pointer-only files, artifact references); re-run R-1 |
| T-8 | `cfxd brief --share` | counts-only, paste-safe receipts so testers can report Brief data |
| T-9 | `cfxd handoff-stats --share` | counts-only Gate 1 + R-1 aggregates from a tester's own logs, for the paper (n ≥ 3 developers) |
| T-10 | `cfxd checkup` | one counts-only summary for a tester's first 10 minutes: prefix size and diet saving, fidelity of recent compactions, MCP churn, sub-agent re-discovery by model, Brief status |
| T-11 | Install smoke on 3 OS in CI | the install script runs from a clean checkout on Windows, macOS and Linux, then `cfxd doctor` |

### Could have (only if time remains)
| ID | Item | Why |
|---|---|---|
| T-12 | Carry v2 tuning at a fixed 3 KB cap | a fair-size comparison against v1; try to reach 2× |
| T-13 | Carry-back two-hook prototype (behind a flag) | only after the founder captures one real `SubagentStop` payload (5 min, free) |
| T-14 | Evidence dump update | add Sprint 12 results and the retired features to `docs/paper/EVIDENCE-DUMP-2026-10-31.md` |

### Not now
Paid runs (Gate 2, v4 registered), B8, a live router, ContextDB, new measurement tools
beyond T-9/T-10.

## Part 3 · Orca plan (two days, cheap builders)

### Roles
| Role | Who | Rules |
|---|---|---|
| Overseer | **Claude Sonnet 5.5**, one coordinator session | dispatches, re-runs every check itself, merges green branches, writes reports. Writes no feature code. Run no paid model calls. Keep the session short: check, merge, move on |
| Builders | **OpenCode** on `opencode-go/deepseek-v4.1-flash` (pinned default; Orca rejects `--model` for OpenCode) | all code and tests |
| Docs/research | **Antigravity (Gemini)** if quota allows, else OpenCode | T-2, T-3 drafts, T-4, T-14 |

Dispatch and checks are the same as `docs/plans/2026-10-31-orca-run.md` (worker template,
chained `&&` checks, key scan, file locks, at most 5 workers, 3 failed cycles → re-dispatch
once → `blocked`). Integration branch: `s13-integration`, created from `main` after T-0.

### Day 1 (2026-10-31)
| Order | WP | Agent | Files (lock) | Acceptance |
|---|---|---|---|---|
| 1 | **T-0** finish R-5 → full checks → merge `s12-integration` into `main`, push | coordinator + R-5 builder | `cfxd/diet.py`, its test | `uv run pytest -q` full suite, ruff, format, mypy and docs-numbers all green on the integrated tree before the push |
| 2 | **T-1** `carry_v2` flag | OpenCode | `cfxd/carry.py`, `cfxd/config.py` (flag only), `tests/test_carry_v2.py` | flag off reproduces v1 exactly (golden test); on gives v2 |
| 2 | **T-2** SECURITY.md | Gemini | `SECURITY.md` | states the carry v2 cap and contents, the Brief v1 excerpt surface and its mitigations, `brief_policy`; coordinator checks each claim against the code |
| 2 | **T-4** feedback forms | Gemini | `docs/FEEDBACK-TEMPLATE.md`, `.github/ISSUE_TEMPLATE/` (old forms only) | old forms point to the Brief templates or are removed; no duplicate forms remain |
| 2 | **T-5** coach sweep | OpenCode | `scripts/bound_coach_replay.py` (sweep mode), `cfxd/coach.py` (defaults only), tests | sweep table in `docs/bound-coach-replay.md`; new defaults chosen by the stated target; aggregates only |
| 2 | **T-6** MCP churn verification | OpenCode | `cfxd/waste.py` (churn rule only), `cfxd/cli.py` (doctor text only), tests | report splits tokens into write/read and dollars, and excludes one expected announcement per session start |
| 3 | **T-3** README evidence | Gemini drafts, coordinator checks numbers | `README.md` (evidence table + status lines only), `docs/engineering/NUMBERS.md` | docs-numbers green; every new number traceable to a script or report; no saving claim |

### Day 2 (2026-11-01)
| Order | WP | Agent | Files (lock) | Acceptance |
|---|---|---|---|---|
| 1 | **T-7** Brief precision | OpenCode | `cfxd/brief_select.py`, its tests | R-1 re-run: precision up, coverage ≥ 85%, packet ≤ 6 KB; before/after table |
| 1 | **T-8** `cfxd brief --share` | OpenCode | `cfxd/cli.py` (brief subcommand only; run after T-6 merges), tests | output has no paths, prompts or text (test with a canary string) |
| 1 | **T-9** `cfxd handoff-stats --share` | OpenCode | `cfxd/handoff_stats.py` (new; wraps `scripts/gate1_rediscovery.py` + `scripts/brief_replay_score.py` logic), CLI entry, tests | output equals the gate1/R-1 aggregates on fixtures; canary test; < 60 s on real logs |
| 2 | **T-10** `cfxd checkup` | OpenCode | `cfxd/checkup.py` (new), CLI entry, tests | composes existing modules only; counts-only; ends with a "safe to paste" line; < 30 s |
| 2 | **T-11** install smoke CI | OpenCode | `.github/workflows/install-smoke.yml` | green on 3 OS |
| 3 | **T-12 / T-13 / T-14** as time allows | OpenCode / Gemini | per item | per item |
| 4 | Merge `s13-integration` → `main`, update `docs/TESTING-GUIDE.md` to add `checkup`, `brief --share`, `handoff-stats --share` (Gemini), write the report | coordinator | — | full checks green; report written |

`cfxd/cli.py` is the bottleneck lock (T-6 → T-8 → T-9/T-10 entries). Dispatch them in
that order, or give T-9 and T-10 their own entry modules and add the CLI lines in one final
small WP.

### Final report (coordinator)
Write it to `docs/plans/2026-11-01-tester-build-report.md`:
- every WP's state, with the checks run
- the before/after tables (coach sweep, MCP churn dollars, Brief precision)
- what changed in SECURITY.md and the README
- the tester build's command list, checked by running each command for real (not only
  `--help`)
- anything blocked, and anything that needs the founder

### Founder tasks (each takes minutes; the agents can't do them)
1. Capture one real `SubagentStop` payload (enables T-13).
2. Send the audit packet to Muse; add its answers to the next run.
3. Approve the README evidence wording (T-3) before it goes to `main`.
4. Tag the alpha release when the tester build report is green.

## After this sprint (tester week)
- Testers run `checkup`, use Brief (opt-in) and coach, and share `week --share`,
  `brief --share` and `handoff-stats --share`.
- Run in parallel during that week: Muse's suggested offline experiments, carry v2
  tuning, and a small paid Gate 2 on Opus/Sonnet children once the Claude limit resets
  and T-7 has raised Brief's precision.
