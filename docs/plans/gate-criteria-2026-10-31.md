# Gate 0: criteria frozen before results are read (2026-10-31)

Written by the overseer. **Status when written:** the E-1…E-4, E-6/E-7 and M-3 builder
branches already existed (their code was built and committed), but the overseer had not
read any of their outputs. Bars below come from the rebuild plan decision rules
(`2026-10-31-feature-rebuild-plan.md`), the Muse response, and the existing registered
documents. Any change after a result is seen needs a dated entry in "Deviations" at the
bottom, with the reason.

## Data split

- **Tuning set:** the founder's own logs (one machine, ~30 days). Thresholds and
  heuristics may be tuned here.
- **Held-out set:** testers' logs, via `handoff-stats --share` and `checkup` (F-1, F-2).
  Nothing is tuned on it. A result counts as replicated only if it clears the same bar
  there without changes.
- Anything computed on the tuning set is labelled "founder machine, exploratory" wherever
  it appears.

## Experiment bars

| ID | Question | Pass | Fail / consequence |
|---|---|---|---|
| E-1 | Is re-discovery a meaningful share of **total session** cost (parent + children)? | Re-discovery cost ≥ 5% of total session cost for sessions with ≥1 Opus/Sonnet child, reported with the denominator shown | < 5%: Brief is not a product headline; keep it opt-in and drop it from tester claims. Between 5% and 10%: "small but real", no marketing line |
| E-2 | Does MCP churn cost real money? | After excluding each server's first announcement, churn priced as cache writes vs reads is ≥ 3% of the founder's 30-day spend **and** ≥ 15 of 20 hand-checked flagged sessions are true churn | Otherwise the doctor warning stays but churn never leads a message |
| E-3 | Is Brief's low precision bloat or a wrong budget? | A budget exists in {2,6,12,20} KB where coverage ≥ 50% of redundant reads (Opus/Sonnet children, parent knew ≥3 files) **and** bytes-weighted precision ≥ 25% | No such budget: Brief stays opt-in, no paid Gate 2 until selection improves and is re-scored. Coverage ≥ 50% only at ≥ 12 KB: not shippable as default |
| E-4 | Does carry v2 select better at equal size? | At both 3 KB and 4 KB caps, v2 restores ≥ 1.5× the dropped edited files of v1 (and ≥ 2× to justify a paid run) | < 1.5× at equal cap: v2 stays off and is not promoted; document the reason selection did not improve |
| E-5 | Does the v4 instrument discriminate? | A deliberately broken handoff (facts removed) scores ≤ 10% on hidden + planted-fact tests; the reference solution scores ≥ 90% | Fix the instrument; no v4 paid run |
| E-6 | Do hooks tax every turn? | p95 of every synchronous hook ≤ 200 ms and p99 ≤ 1 s on the founder machine | Over bar: that hook goes async or off by default |
| E-7 | Do receipts or state hold prompt/tool text? | Zero stored excerpts, prompts, or tool output in the state dir (canary scan finds none); the Windows ACL denies other users | Any hit: stop-ship, fix before testers. ACL not restricted: document as a single-user-machine assumption (M-5) and fix if cheap |

## Decision rules carried over (unchanged)

- **Brief paid Gate 2:** only if E-3 passes at ≤ 6 KB.
- **Carry v2 paid confirmation:** only if E-4 shows ≥ 2× at equal cap.
- **Coach on for testers:** only with the random-half control (F-4) and precision ≥ 80%.
  Current replay: precision 100%, recall 35%, which is below the recall bar to claim it
  catches costly sessions, so it ships off and as a measured experiment.
- **Bound / fold / checkpoint-restart:** retired (ADR-0012); no bar, no work.
- **Diet:** may be described as an **estimate** (arithmetic over the user's own setup).
  No dollar saving is claimed until R-5's delta is checked on a second machine.

## Tester-build stop-ship bars (M-items)

Each M-item is done only with evidence in the tester-build report:

- M-1: flag off reproduces v1 output byte for byte (golden test).
- M-2: one test asserts every shipped default; docstring and guide match it.
- M-3: canary strings never appear in any `--share` output; digests only behind
  `--share-digests`.
- M-4: a test proves excerpts come only from line ranges the parent read; no excerpts for
  files outside the git work tree; default off.
- M-8: accounting code has hand-computed golden transcripts; CI replay check passes.
- M-11: install-smoke green on Windows, macOS, Linux at the tagged commit.

## What may be claimed publicly

- Nothing from the v2 benchmark as a saving (voided; see the confound deviation).
- Nothing from E-1…E-4 until it also clears the bar on the held-out set, except as
  "founder machine, exploratory".
- No number reaches the README or paper without a golden test (M-8) and the replay check.

## Deviations

(none yet)
