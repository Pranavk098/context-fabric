# B4 v3 Result — the repo does not fit the window: **MIXED** (both models); KILL on both under corrected scoring

Pre-registration: `docs/engineering/B4-V3-PREREGISTRATION.md` (frozen 2026-10-19 03:20 UTC).
Objectives: written blind by a separate agent, `evals/fixtures/b4v3_objectives.json`, unchanged
since `acf246a`. Runs were executed by an Orca-supervised OpenCode worker (dispatch
`ctx_fde0e2d7ed51`), with zero errors in every arm. The coordinator then recomputed both verdicts
from the per-request records and they match.

Receipts: `evals/reports/b4_v3_20261018_202752.json` (1.5B), `evals/reports/b4_v3_20261018_202945.json`
(0.5B); full stdout in `evals/reports/b4_v3_run_Qwen2.5-*.log`.

Setup: the full httpx package (23 files, ~64.8k tokens) against a 16,384-token window, so it does
not fit. 20 objectives × 1 repeat, `temperature=0`, `max_tokens=96`. Arms: **T** truncate the
package to fit; **R** BM25 over AST chunks, 4,000-token budget; **C** compile (mechanical fabric +
shared Tier-0 cartridge + terse contract + exact import lines), 4,000-token budget.

## Numbers

| model | arm | correct (of 20) | billed prompt tokens | uncached tokens | peak KV |
|---|---|---:|---:|---:|---:|
| 1.5B | T truncate | 10 | 325,700 | 65,908 | 13.1% |
| 1.5B | R retrieval | 9 | 80,956 | 75,212 | 3.3% |
| 1.5B | **C compile** | **7** | **57,499** | **46,907** | **2.5%** |
| 0.5B | T truncate | 4 | 325,700 | 65,908 | 3.5% |
| 0.5B | R retrieval | 5 | 80,956 | 75,212 | 0.9% |
| 0.5B | **C compile** | **3** | **57,499** | **46,907** | **0.6%** |

Verdict per model, against the frozen criteria:

- tokens (`C billed ≤ T billed / 3`): **pass** (57,499 vs 325,700 → 5.66× fewer).
- quality (`C ≥ R` and `C ≥ T − 2`): **fail** on both models.
- KILL requires `C < R − 2`: 7 vs 9 and 3 vs 5 are both exactly at the line, so the verdict is
  **MIXED**, not KILL.

## Scorer defect, found after the runs (the verdict of record is unchanged)

The frozen token rule keeps `.` inside tokens, so an answer ending a sentence with `…is 5.0.`
tokenizes to `5.0.` and misses the required `5.0`. Re-scoring every recorded answer with only that
fix (strip leading/trailing `.`, `-` and `/` from each token):

| model | T | R | C | verdict under the fix |
|---|---:|---:|---:|---|
| 1.5B | 10 | **11** | 7 | **KILL** (7 < 11 − 2) |
| 0.5B | 4 | **6** | 3 | **KILL** (3 < 6 − 2) |

The changed answers are all in arm R (1.5B: `b4v3-i1`, `b4v3-r1`; 0.5B: `b4v3-r1`); no arm-C or
arm-T answer changes. Under correct scoring, compile loses to plain retrieval by more than the
tolerance on both models. The frozen verdict is MIXED only because the bug cost the retrieval
baseline answers. Per §8 this is reported, not re-run, and the frozen receipts are not edited.

## What holds and what does not

- **Holds — cost:** when the repo does not fit, compile is the cheapest arm on every cost measure:
  5.66× fewer billed tokens than truncation, the lowest uncached prefill (46,907 vs 65,908 T and
  75,212 R, i.e. 29% and 38% less), the lowest peak KV, and the lowest latency (0.28 s vs 1.09–1.22 s
  on 1.5B). This is the first B4 run where compile beats every baseline on uncached tokens.
- **Does not hold — accuracy:** at the same 4,000-token budget, plain BM25 over code chunks answers
  more questions than the compiled packet on both models. "Same accuracy for fewer tokens" is
  **not** supported for small local models, even when the repo does not fit.
- **Scale caveat:** every arm is weak (≤ 11/20 on 1.5B, ≤ 6/20 on 0.5B). These models cannot carry
  the task well under any context strategy, so the comparison says little about larger models. The
  3B model could not serve a 16,384-token window on this 8 GB GPU (§3 of the pre-registration).

## Per-objective passes (1.5B; T/R/C, frozen scorer)

| objective | T | R | C |
|---|:-:|:-:|:-:|
| b4v3-i1 | · | · | ✓ |
| b4v3-i2 | ✓ | ✓ | · |
| b4v3-i3 | ✓ | · | ✓ |
| b4v3-i4 | ✓ | ✓ | · |
| b4v3-i5 | · | ✓ | ✓ |
| b4v3-p1 | · | · | · |
| b4v3-p2 | · | · | · |
| b4v3-p3 | · | · | · |
| b4v3-p4 | ✓ | · | · |
| b4v3-p5 | ✓ | ✓ | ✓ |
| b4v3-r1 | · | · | · |
| b4v3-r2 | · | · | · |
| b4v3-r3 | ✓ | ✓ | ✓ |
| b4v3-r4 | ✓ | ✓ | · |
| b4v3-r5 | · | ✓ | ✓ |
| b4v3-t1 | · | · | · |
| b4v3-t2 | · | · | ✓ |
| b4v3-t3 | ✓ | ✓ | · |
| b4v3-t4 | ✓ | ✓ | · |
| b4v3-t5 | ✓ | · | · |

Any follow-up experiment (a fixed scorer, a larger model on bigger hardware, a different budget)
needs its own, separately dated pre-registration.
