# Pilot re-run on mechanical memory — result

Pre-registration: `docs/engineering/PILOT-MECH-PREREGISTRATION.md` (frozen 2026-10-18 22:07 UTC).
It is identical to the original pilot except for one change: the compile arm's 11 hand-derived
blocks, each carrying its answer key, are replaced by `evals/mech_fabric.build_blocks` over the
same 10 httpx files (145 blocks, built without reading objectives or keys).

Result of record: `evals/reports/pilot_20261018_151640.json` (smoke:
`pilot_smoke_20261018_151633.json`). Model: Qwen2.5-1.5B-Instruct, vLLM with prefix caching,
`--max-model-len 32768`, RTX 5070 Laptop 8 GB.

## Verdict: all three pre-registered criteria PASS

| criterion | result |
|---|---|
| (a) compile sends ≥3× fewer prompt tokens | **PASS**: 193,366 vs 14,193 → **13.62×** |
| (b) accuracy parity (compile ≥ raw − 1) | **PASS**: 5/6 vs 5/6 |
| (c) no compile errors | **PASS**: 0 errors, 6/6 compiled |

## Effect of the fabric change

| | original pilot (hand blocks) | this run (mechanical) |
|---|---:|---:|
| compile prompt tokens (6 requests) | 5,391 | 14,193 |
| billed-token ratio vs raw | 35.87× | **13.62×** |
| accuracy | 5/6 vs 5/6 | 5/6 vs 5/6 |

Most of the 35.87× came from hand-writing compact blocks that carried the answers. With
mechanically built memory the reduction is 13.62×, and accuracy still holds.

## What the billed-token ratio hides (reported, not a criterion)

The raw arm sends the same 32k-token system message every time, so vLLM's prefix cache serves
almost all of it:

| arm | prompt tokens | prefix-cache hits | **uncached tokens** | peak KV |
|---|---:|---:|---:|---:|
| raw (6 requests) | 193,366 | 193,184 | **182** | 25.9% |
| compile (6 requests) | 14,193 | 2,336 | **11,857** | 1.9% |

- On a warm cache, raw pays far less prefill than compile (182 vs 11,857 uncached tokens).
  The smoke pass had already paid raw's cold 32,227-token prefill. Counted over the whole
  session (smoke + full), raw's uncached total is about 32.4k against compile's about 14.2k.
- Compile's durable advantages here are **context size** (2.3k vs 32k tokens per request) and
  **KV footprint** (about 13× lower peak), which matter for concurrency and for contexts that
  would not fit. Billed tokens also matter on providers that charge for cached reads; see
  `docs/engineering/COSTS.md`.
- This matches B4 (`docs/engineering/B4-RESULT.md`): against a warm shared prefix, compiling
  does not reduce prefill work.

## Iterations (tooling only, disclosed)

1. `pilot_20261018_151404.json` (and smoke `pilot_smoke_20261018_151403.json`): every request
   failed with 502. Cause: the orchestrator's run script sent its engine warm-up request
   without `temperature=0.0`, so vLLM took the random-sampling path, whose FlashInfer kernel
   crashed the engine (`EngineDeadError`). The established `scripts/wsl_run_eval.sh` warms up
   at `temperature=0.0`, and the pilot itself uses 0.0. Fix: warm-up at `temperature=0.0`;
   nothing else changed.
2. Harness bug found in that failed run: criteria (a) and (b) evaluated **PASS** on an
   all-errors run (`0 ≤ 0/3`, `0 ≥ 0 − 1`). Only (c) caught it. `_criteria` now fails (a) when
   no tokens were measured and fails (b) when any request errored. Regression test:
   `tests/test_pilot_mech.py::test_all_error_run_fails_every_criterion`. The verdicts of the
   original receipt and of this run's receipt are unchanged (all PASS).
