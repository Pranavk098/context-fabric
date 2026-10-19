# SKIM-MODEL — model-based code skimmer for hand-offs (B8b follow-up)

**Status: EXPLORATORY. Nothing in this file is a claim of record.** The tables below are
recall diagnostics on the B4 v3 dev set (`evals/fixtures/b4v3_objectives.json`, frozen
sha256). Over-fitting here is expected; a later, separately pre-registered experiment on
held-out objectives would be required to claim any accuracy win.

## What it is

`kernel/router/skim_model.py` adds two local-GPU skimmer tracks behind flags (default OFF,
so the core package and its tests never import torch). Both re-use the `kernel/router/skim.py`
AST chunker plus a shared **source-order 0/1 knapsack assembler**:

- **SKIM-PPL (Track A, training-free)** — contrastive perplexity of each code block given the
  objective, from `Qwen/Qwen2.5-0.5B-Instruct` (causal LM, cached WSL):
  `score(block) = PPL(block) − PPL(block | objective)`, computed in two batched forward passes
  per length-bucket (`logsumexp` + gather, no full `log_softmax` materialisation).
- **SKIM-PRUNER (Track B, 634M token-classification)** — per-line keep-probabilities from
  `ayanami-kitasan/code-pruner` (SWE-Pruner, MIT; architecture vendored into `skim_model.py`
  and loaded directly, no `swe-pruner` package), averaged over each block.

Both always keep `def`/`class` signature lines, `return`/`raise` lines, and import lines that
bind names used in kept lines, then fill the remaining budget with an exact 0/1 knapsack DP
over AST blocks, re-emitting in original source order under `file:line` anchors. A structural
fix the frozen B4 v3 chunker lacks is included: **module-level `Assign`/`AnnAssign` constants
(`DEFAULT_MAX_REDIRECTS = 20`, etc.) are always a candidate block**, which the old chunker
dropped entirely.

## How to run

```sh
# R / SKIM / C (CPU, offline)
python -m evals.b8_skim_offline --fixture <httpx_clone> --dry-run

# add the two model arms (local GPU; WSL: ~/.venvs/vllm)
python -m evals.b8_skim_offline --fixture <httpx_clone> --dry-run --model-arms
```

Unit tests use a stub scorer (no GPU): `pytest tests/test_skim_model.py`. Receipts land under
`evals/reports/b8_skim_model_*.json`.

## Results (dev set, 20 objectives / 33 required facts)

Tokenizer: Qwen2.5-0.5B-Instruct (the `_Counter` the R/SKIM/C arms use in this run — so the
baseline here differs slightly from `SKIM.md`, whose table was tiktoken-flagged). Recall is
the fixed-scorer token match (leading/trailing `./-/` stripped).

| arm | budget | prompt tok (20 reqs) | max/req | obj. all facts | fact coverage |
|---:|---:|---:|---:|---:|---:|
| R (BM25 chunks) | 1k | 20,524 | 1,040 | 9 / 20 | 19 / 33 |
| SKIM | 1k | 20,329 | 1,032 | 8 / 20 | 19 / 33 |
| C (old compile) | 1k | 22,267 | 1,156 | 12 / 20 | 22 / 33 |
| **SKIM-PPL** | 1k | 19,780 | 1,003 | 7 / 20 | 14 / 33 |
| **SKIM-PRUNER** | 1k | 19,710 | 1,003 | 6 / 20 | 13 / 33 |
| R | 2k | 40,490 | 2,039 | 15 / 20 | 27 / 33 |
| SKIM | 2k | 40,128 | 2,025 | 12 / 20 | 23 / 33 |
| C | 2k | 35,057 | 1,852 | 15 / 20 | 26 / 33 |
| **SKIM-PPL** | 2k | 38,887 | 1,961 | 8 / 20 | 15 / 33 |
| **SKIM-PRUNER** | 2k | 38,737 | 1,956 | 6 / 20 | 13 / 33 |
| R | 4k | 80,516 | 4,039 | 17 / 20 | 29 / 33 |
| SKIM | 4k | 79,811 | 4,013 | 12 / 20 | 23 / 33 |
| C | 4k | 57,099 | 3,036 | 16 / 20 | 28 / 33 |
| **SKIM-PPL** | 4k | 77,227 | 3,876 | 8 / 20 | 15 / 33 |
| **SKIM-PRUNER** | 4k | 76,941 | 3,869 | 9 / 20 | 17 / 33 |

## Latency per hand-off (RTX 5070, WSL2)

| arm | cold ms (mean / p95) | warm ms (mean) |
|---|---:|---:|
| **SKIM-PPL** | 3,172 / 3,662 | 85.5 |
| **SKIM-PRUNER** | 4,543 / 5,747 | 87.2 |
| BM25 (`R`) | ~few ms (CPU) | ~few ms |

"Cold" = first budget for an objective (includes model scoring of ~740 blocks across the top-12
chunks); "warm" = later budgets reuse the cached per-objective scores (knapsack only). Both
model arms are well over the 500 ms exploratory target from `SKIMMER-RESEARCH.md`.

## Verdict

**No — neither track beats R.** On the dev set, SKIM-PPL recalls 14/15/15 facts and
SKIM-PRUNER 13/13/17, versus R's 19/27/29 at 1k/2k/4k, while costing ~3.2–4.5 s cold latency
per hand-off (R is a few ms). Both model tracks lose on recall at *equal* budget and are
~1000× slower.

Two things go wrong, and they compound. (1) The zero-shot block scores do not separate
fact-bearing lines from boilerplate: on the `FOUND`/`SEE_OTHER` and `IM_A_TEAPOT` objectives,
the top-scoring blocks were type aliases (`ResponseExtensions = Mapping[str, Any]`), `TypeVar`
bindings, and docstrings, while the status-code constant block and `_redirect_method` scored
mid-pack. (2) The 0/1 knapsack, fed those scores at block granularity, packs the budget with
many small high-score boilerplate blocks and drops the larger interior blocks (methods,
class-level assignments) that whole-chunk BM25 keeps. The module-constants fix does work — it
recovers `20`, `DEFAULT_MAX_REDIRECTS`, etc. that R misses — but that gain (~2–4 facts) is
swamped by ~8–14 interior-fact losses. A future attempt should keep whole chunks and only
*prune* clearly-irrelevant lines, and use the constants fix as a low-risk add-on rather than
trusting zero-shot block scores for selection.
