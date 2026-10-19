# B8 Live Smoke — Result (astropy__astropy-7606)

Task `astropy__astropy-7606` (repo `astropy/astropy`), model `gpt-4.1-mini`, 1 task × 3 arms, graded.
Window cap 24,576 · handoff budget 8,192 · step limit 35.

## Per-arm results (clean runs)

| Arm | exit_status | overflow | steps | billed | cached | uncached | resolved | tok/step | wall_s |
|-----|-------------|----------|-------|--------|--------|----------|----------|----------|--------|
| A   | ContextWindowExceededError | yes | 31 | 491,127 | 442,240 | 48,055 | 0 | 15,842.8 | n/a¹ |
| B   | Submitted | no | 11 | 82,423 | 77,952 | 2,763 | 1 | 7,493.0 | 29.267 |
| C   | LimitsExceeded | no | 35 | 395,272 | 346,112 | 48,230 | 0 | 11,293.5 | 40.482 |

¹ Arm A ran in the first (600,000-cap) invocation whose receipt was lost to the grading crash; its wall time was not captured.

- **Arm A** (naive dump, no fold) overflowed the 24,576 window cap at step 31 — the designed failure mode.
- **Arm B** (current CF packet) resolved at step 11, cheapest (82,423 tokens).
- **Arm C** (BM25 skim + obs-fold) ran the full 35-step limit without overflowing (fold held the window) but did not resolve.

## Ledger (day 2026-10-19)

- total **1,074,961** (prompt 1,070,703, completion 4,258; cached 961,408, uncached 109,295).
- Includes **106,139 wasted tokens** from Arm B's first run, which was cut by the 600k cap at step 14 (this is on top of the clean A/B/C numbers above). Clean triplet = 968,822 tokens.

## Harness projection (receipt `evals/reports/b8_smoke_20261019_112005.json`)

- `projected_total_tokens_30` = **32,248,830** tokens, **15 days** at 2,200,000/day (based on the full ledger incl. the 106k waste; clean-triplet projection is ~29.06M → 14 days).

## Sizing table — projected total tokens / days @ 2.2M/day (linear in measured tokens-per-step)

tokens-per-step: A 15,842.8 · B 7,493.0 · C 11,293.5 (sum 34,629.3)

| n \ step_limit | 20 | 25 | 35 |
|---|---|---|---|
| **10** | 6,925,858 / 4d | 8,657,323 / 4d | 12,120,252 / 6d |
| **15** | 10,388,788 / 5d | 12,985,985 / 6d | 18,180,378 / 9d |
| **20** | 13,851,717 / 7d | 17,314,646 / 8d | 24,240,505 / 12d |

## What was fixed / found along the way

1. **`mcp` missing** — mini-swe-agent's first model call raised `ModuleNotFoundError: No module named 'mcp'` (litellm 1.106.0 imports MCP handlers whenever `tools` is passed). Installed `mcp 2.3.0`; added to `scripts/wsl_b8_setup.sh` and `docs/engineering/B8-HARNESS.md` (setup + recorded versions).
2. **Grading crash** — `KeyError: 'eval_script'` in `make_test_spec`: the `princeton-nlp/SWE-bench_Verified` mirror dropped `eval_script`/`log_parser`/`eval_type`. Switched `DATASET` to `SWE-bench/SWE-bench_Verified` (same 500 instance ids, retains those columns + `image`).
3. **Daily-cap retry loop** — the cap 429 was retried 10× by mini-swe-agent. Now the sidecar returns **400 `daily_cap_reached`** (non-retryable), the runner pre-checks the ledger before each task×arm and marks a cap stop as `cap_stopped` (resume tomorrow), and the sub-agent model aborts on `BadRequestError`.
4. **CLI knobs** — added `--step-limit` and `--handoff-budget` (defaults unchanged); the receipt records them.

Files changed (uncommitted): `sidecar/b8.py`, `sidecar/server.py`, `evals/b8_live.py`, `tests/test_b8.py`, `scripts/wsl_b8_setup.sh`, `docs/engineering/B8-HARNESS.md`. Tests: `tests/test_b8.py` → 15 passed.
