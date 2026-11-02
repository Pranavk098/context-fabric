# Wave decisions and frozen P4 bars (2026-11-01)

Committed before Wave 1 dispatch. A changed bar after this commit is a new dated note, never an edit.

## 3. Founder decisions (decided 2026-11-01)

| ID | Decision |
|---|---|
| D-1 | `coach_control` **on** by default in the tester build. Tester onboarding (W2-03) and the feedback form say plainly: "You are in a measurement study. Half of testers see coach prompts and half do not; which half you are in is fixed by a hash of your install." Consent to publish aggregates is not consent to the study, so the form asks both separately |
| D-2 | M3 bar: at least **25% of 40+ turn sessions have a break-even turn with positive headroom** (cost-to-finish restarted < cost-to-finish continuing, net of prefix re-pay and packet). Existence of a break-even turn alone does not count. This is gate 1 of 2 for P7; gate 2 is the coach treatment arm showing a behaviour difference in W4 |
| D-3 | Plan-tier denominators only from an official published source, labelled *estimate* with the source URL and date in `limit_weights.toml`. **Fallback: if no official figure exists for a tier, show limit-weighted tokens with no limit share for that tier.** An invented limit is never shown |
| D-4 | No number chosen blind. W3-05 runs a 3-task probe (v4 pilot precedent) per arm; ceiling = probe cost per task × tasks × arms × 1.5, written into the pre-registration with the stop rule: stop at the ceiling, report partial results, no silent overrun |
| D-5 | Codex reader ships behind `--source codex`, labelled beta, with a golden fixture, **only if W1-09 returns "reader feasible" with real per-message usage data.** If W1-09 returns "partial (no usage)", the reader reports counts only with no cost or limit figures, or it does not ship |

**P4 bars, frozen** (committed in W0 before any measurement runs; this file does not move them):

| Result | M1 (total elidable repeat-share of new tokens) | Consequence |
|---|---|---|
| ≥ 15% | lead | W2-05 tracker and W3-03 dedupe build as a feature; dedupe may lead the tester narrative after the paired run |
| 5–15% | quiet | W2-05 and W3-03 build **only as off-by-default experiments with no tester claim**, not in any guide or README headline |
| < 5% | demote | W2-05 and W3-03 park; dedupe becomes a framework-track experiment only |

M3 uses D-2. W2-05 also starts if M3 clears D-2 even when M1 < 5% (the tracker serves P7's packet).

The orchestrator commits these decisions and bars to `docs/plans/2026-11-01-wave-decisions.md`
**before** W1 dispatch. The commit hash is cited in every P4 results note. A changed bar after that
commit is a new, dated decision note, never an edit.

**The tracker (W2-05) is not a framework dependency.** If it parks, the framework track is not
blocked; its tracker just has no plugin prototype to learn from.

## Overnight authority (decided 2026-11-01, before the 12-14 hour sprint)

| Topic | Decision |
|---|---|
| Release | The orchestrator may merge `w-integration` into `main`, push, and tag `v0.1.1-alpha.2` only if the full suite, ci, plugin, macos, replay-check and install-smoke (3 OS) are green on `main`. Any red: fix forward, never move a tag. No announcement, no tester invitation |
| SECURITY.md text | Security code and tests ship. The SECURITY.md rewrite stays out of pushed `main` until the founder approves it; the existing SECURITY.md remains |
| Paid runs | Up to $10 total for the W3-05 probe (Haiku). Nothing else is paid |
| OpenCode usage | `scripts/opencode_budget.py` before and after each wave. At WARN ($15/week) remaining small WPs go to Gemini/Antigravity and the orchestrator only reviews and merges; at STOP builders halt and a status report is written |
