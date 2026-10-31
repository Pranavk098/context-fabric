# Delivery status and the tester-build sprint (2026-10-31 → 2026-11-01)

Part 1 is what's built and whether it works. Part 2 is what's left. Part 3 is the Orca
plan to deliver it in two days on cheap builders. **Parts 2–3 were revised after Muse's
audit (2026-10-31); see `docs/audit/2026-10-31-muse-response.md`.** External audit questions are in
`docs/audit/2026-10-31-muse-audit-packet.md`.

## Part 1 · What's built, and is it doing its job?

Legend: ✅ works as intended · 🟡 works, effect unproven or below bar · ❌ disproven or
broken · ⏸ parked

| Area | Feature | State | Verdict | Keep for testers? |
|---|---|---|---|---|
| Measure | `audit`, `diet` report, `waste`, `week --share`, `ledger`, `stats` | main | ✅ | yes |
| Measure | `fidelity` (what /compact forgets), `refetch` | main | ✅ one machine | yes; second act after diet |
| Measure | `routing` by child model (R-9) | s12-integration | ✅ price only | yes |
| Measure | MCP churn rule + doctor warning (R-7) | s12-integration | 🟡 dollar split unchecked | yes, after T-6 |
| Act | Brief v1 + ordering fix + policy (R-2) | main + s12 | 🟡 covers 92% of re-reads, precision 11.8%, no paid test | yes, opt-in |
| Act | `cfxd brief` receipts view | main | ✅ | yes |
| Act | Carry v1 (post-compact facts) | main | ✅ functions; effect unmeasured | yes |
| Act | Carry v2 (R-4) | s12-integration | 🟡 1.4× v1 (bar 2×); **live without a flag** | behind flag (T-1) |
| Act | Diet `--apply/--undo` | main | ✅ deterministic; dollar estimate pending (R-5) | yes; **lead feature** (Muse) |
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


## Part 2 · What's still to deliver before testers (revised after Muse)

Muse's verdict: **not tester-ready yet, but close. The gap is a checklist, not a
redesign.** The job of the tester build is to show that **our measurement works on
someone else's machine**, not that Brief works. Priorities below follow that.

**Authorship rule (Muse):** SECURITY.md, deviation docs, gate criteria and any number
that could reach the README or paper are written by the Sonnet overseer (or the
founder), never by a cheap builder. Builders write code and tests.

### Gate 0 · Freeze the criteria before any tool runs (overseer, first hour)
`docs/plans/gate-criteria-2026-10-31.md`: the pass/fail bar for every experiment E-1…E-7
and every decision in Part 2, written **before** any of them runs. Declare the split:
the founder's logs are the tuning set, and testers' logs are the held-out set. No bar
changes after a result is seen without a dated deviation entry.

### Must have: stop-ship list (no tester build without all of these)
| ID | Item | Source |
|---|---|---|
| M-0 | Finish R-5 (diet delta; label it an **estimate** if pricing is unsure), full checks on `s12-integration`, merge to `main`, push | Muse: Sprint 12 must be on main |
| M-1 | `carry_v2` flag, default off; **golden test**: flag off reproduces v1 byte for byte | Muse risk 4 |
| M-2 | **Defaults test + doc fix.** One test asserts every shipped default (`brief` v0 on, `brief_v1` off, `brief_policy` on, `carry` on, `carry_v2` off, `carry_back` off, `coach` off, `bound` off, `fold` off). Fix the `cfxd/config.py` docstring and the guide so they say exactly that | Muse: the default contradiction |
| M-3 | **`--share` numeric-only by default** for `week`, `diet`, `ledger`, `brief`. Name digests only behind an explicit `--share-digests` (private use). Canary-string tests on every share path | Muse security answer |
| M-4 | **Brief excerpt provenance hardening.** Excerpts only from line ranges the parent actually read (asserted in a test); skip files outside the git work tree; per-repo opt-in for excerpts (`brief_excerpts`, default off, pointers-only without it) | Muse risk 3 |
| M-5 | SECURITY.md refresh (overseer writes): carry v2 4 KB + constraints/ranges, Brief excerpt surface + M-4 mitigations, numeric `--share`, masking limits, Windows ACL as a **documented single-user-machine assumption** (plus E-7's check) | Muse |
| M-6 | README fixes (overseer writes; founder approves): the benchmark line says v2 ran and was **voided** (link the deviation); v4 unregistered; retired features listed as retired; no saving claim | Muse |
| M-7 | **Retired means retired.** Remove fold, bound and checkpoint-restart from the guide, `cfxd tour`, `--help` listings and the `/checkpoint` command docs (commands print "retired, see ADR-0012"). Carry-back stays a research note; the X1 demo is docs-only | Muse cut list |
| M-8 | **Golden-transcript tests** for all accounting code: dedup, Gate 1 join, R-1 scorer, churn rule, diet delta. A fixed transcript with hand-computed expected values. Plus a **replay check** in CI that recomputes one known report and diffs it | Muse process (a)(b) |
| M-9 | Tester feedback form (overseer drafts): counts-only report upload, session/child counts by model, task and repo type, one Likert per feature ("did Brief/Carry change anything you noticed?"), a friction log (install, daemon, false warnings), **explicit consent** to aggregate and publish counts | Muse |
| M-10 | One-page guide per tester feature (diet, fidelity, `checkup`, Brief opt-in session) | Muse |
| M-11 | **Tagged build.** Testers install a tag, not a branch, through the install script; the install-smoke CI must be green on 3 OS at that tag | Muse |
| M-12 | **Founder:** named owner for tester-week triage with stop-ship authority | Muse |

### Offline experiments (no model spend; run against frozen Gate 0 criteria)
| ID | Experiment | Decides |
|---|---|---|
| E-1 | **Denominator tool**: parent vs child cost split; re-discovery as a share of **total session** cost, not child cost | Whether Brief can ever be a product claim (Muse risk 1) |
| E-2 | **MCP churn dollars + false positives**: price the 3,103 re-announcements as cache write vs read; exclude the expected first announcement; hand-check 20 flagged sessions | Whether churn can lead anything |
| E-3 | **Brief precision–recall curve**: R-1 at 2/6/12/20 KB, bytes-weighted, per child family | Budgets by evidence; whether low precision is bloat or a wrong budget. Replaces the "Brief precision pass" |
| E-4 | **Carry at a fixed budget**: v1 vs v2 at 3 KB and at 4 KB; restores per KB | Whether v2's selection is better at all (Muse: 11.0 vs 11.7 restores per KB) |
| E-5 | **v4 instrument validation**: hidden + planted-fact tests against a deliberately broken handoff (reference solution with facts removed) must score ~0; no model calls | The v4 quality check before any paid run |
| E-6 | **Hook latency audit**: p50/p95/p99 per hook from real daemon receipts | Whether hooks tax every turn |
| E-7 | **Receipt content + ACL audit**: scan the state directory for stored excerpt or tool text (expect none); check the Windows ACL on the state dir denies other users | Closes the encryption-at-rest question; backs M-5 |

### Tester features (after the stop-ship list)
| ID | Item | Note |
|---|---|---|
| F-1 | `cfxd handoff-stats --share` (numeric): Gate 1 + R-1 + E-1 aggregates from a tester's own logs | **Top priority**: out-of-sample replication of Gate 1 is the single result that most changes perception (Muse) |
| F-2 | `cfxd checkup` (numeric, ends "safe to paste"), ordered **diet first**, fidelity second, then sub-agent re-discovery by model and Brief status. MCP churn only after E-2 verifies | Muse: lead with diet |
| F-3 | `cfxd brief --share` (numeric) | part of M-3 |
| F-4 | Coach: keep precision-first thresholds; add an optional **random-half control** (warn half the qualifying sessions, log both) so tester week measures whether a warning shortens or cheapens sessions | Muse: an unmeasured nudge is decoration |
| F-5 | Install smoke CI on 3 OS + time-to-first-report measured | Muse: install success rate |

### Later (not this sprint)
Gate 2 (paid, Opus/Sonnet children) with **one calibration run** (does offline
"avoidable" predict paid savings?); the v4 registered run with condition D **plus a
relay condition**, a fixed delegation prompt and a pre-registered non-inferiority
quality bound; model-tier routing (Sonnet child doing an Opus child's job) as the
highest-upside experiment, behind Gate 2 with a quality-parity design; carry-back
two-hook prototype; paper dataset target ≥5 developers, ≥200 sessions, ≥1,000 children.

## Part 3 · Orca plan (two days, cheap builders, Sonnet 5.5 oversight)

Roles, dispatch commands, worker template and checks are unchanged from
`docs/plans/2026-10-31-orca-run.md`: the Sonnet 5.5 overseer only dispatches, checks,
merges and writes the overseer-authored docs; builders are OpenCode
(`opencode-go/deepseek-v4.1-flash`) or Antigravity/Gemini. At most 5 workers. Integration
branch `s13-integration`, cut from `main` after M-0. **New:** every PR that produces a
number must ship its golden test (M-8) and pass the replay check before merge.

### Day 1 (2026-10-31)
| Slot | WP | Who | Files (lock) |
|---|---|---|---|
| 0 | Gate 0 criteria doc | overseer | `docs/plans/gate-criteria-2026-10-31.md` |
| 1 | M-0 R-5 + merge Sprint 12 to `main` | R-5 builder + overseer | `cfxd/diet.py`, its test |
| 2 | M-1 carry_v2 flag + golden | OpenCode | `cfxd/carry.py`, `tests/test_carry_v2.py` |
| 2 | M-2 defaults test + docstring | OpenCode | `cfxd/config.py` (docstring only), `tests/test_defaults.py` (new) |
| 2 | M-8 golden transcripts + replay CI | OpenCode | `tests/golden/` (new), `tests/test_golden_accounting.py` (new), `.github/workflows/replay-check.yml` (new) |
| 2 | E-1 denominator | OpenCode | `scripts/denominator.py`, test, `docs/denominator.md` |
| 2 | E-2 MCP churn dollars | OpenCode | `scripts/mcp_churn_dollars.py`, test, `docs/mcp-churn-dollars.md` |
| 3 | E-3 Brief PR curve | OpenCode | `scripts/brief_replay_score.py` (budget sweep), `docs/brief-replay-score.md` |
| 3 | E-4 carry fixed budget (after M-1) | OpenCode | `scripts/carry_fidelity_score.py` (cap option), `docs/carry-fidelity-score.md` |
| 3 | E-6/E-7 latency + state/ACL audit | OpenCode | `scripts/hook_latency.py`, `scripts/state_audit.py`, tests, `docs/state-and-latency-audit.md` |

### Day 2 (2026-11-01)
| Slot | WP | Who | Files (lock) |
|---|---|---|---|
| 1 | M-3 numeric `--share` (week, diet, ledger, brief) | OpenCode | `cfxd/week.py`, `cfxd/diet.py` (share path), `cfxd/ledger.py`, `cfxd/cli.py` (share flags), `tests/test_share_numeric.py` |
| 1 | M-4 excerpt provenance + opt-in | OpenCode | `cfxd/brief_render.py`, `cfxd/config.py` (`brief_excerpts`), `tests/test_brief_provenance.py` |
| 1 | E-5 v4 instrument validation | OpenCode | `evals/plugin_ab/validate_v4_instrument.py`, test |
| 2 | F-1 `handoff-stats --share` | OpenCode | `cfxd/handoff_stats.py` (new), test; CLI line added in F-2 |
| 2 | F-2 `checkup` (+ the F-1 CLI line), after M-3 | OpenCode | `cfxd/checkup.py` (new), `cfxd/cli.py` (entries only), tests |
| 2 | F-4 coach control, after M-4 | OpenCode | `cfxd/coach.py`, `cfxd/config.py` (`coach_control`), tests |
| 2 | M-7 retire from UI, after F-2 | OpenCode | `cfxd/tour.py`, `commands/checkpoint.md`, `cfxd/cli.py` (help text) |
| 3 | M-5 SECURITY.md, M-6 README, M-9 form (overseer writes); M-10 one-pagers + guide update (Gemini drafts, overseer checks) | overseer + Gemini | `SECURITY.md`, `README.md`, `docs/TESTING-GUIDE.md`, `docs/testers/`, `.github/ISSUE_TEMPLATE/` |
| 4 | F-5 install smoke + M-11 tag readiness | OpenCode + overseer | `.github/workflows/install-smoke.yml` |

Lock order: `cfxd/cli.py` M-3 → F-2 → M-7; `cfxd/config.py` M-2 → M-4 → F-4;
`cfxd/diet.py` M-0 → M-3; carry M-1 → E-4.

### Final report (overseer)
Write it to `docs/plans/2026-11-01-tester-build-report.md`:
- the Muse stop-ship checklist (M-0…M-12), each ticked with evidence
- E-1…E-7 results against the Gate 0 criteria
- each tester command run for real at the candidate tag
- what's blocked, and what the founder must do

The tag itself is the founder's call.
