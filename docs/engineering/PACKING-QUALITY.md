# PACKING-QUALITY — diversity-aware selection and U-curve placement (2026-10-17)

Two gap-closure changes to how Context Fabric fills a packet, plus the measurements that
come with them:

1. **λ-MMR selection** in `kernel/router/packer.py` — block selection inside a tier is no
   longer plain utility-greedy.
2. **U-curve placement** in `sidecar/render.py` — the render order is now explicit and the
   tail carries a `KEY CONSTRAINTS` recap of the most recent decision/negative-knowledge
   blocks.

Every number below is produced by a committed, deterministic script and stored under
`evals/reports/` (git-ignored): claims are reproducible, artifacts are not part of the
repo.

## 1. λ-MMR selection

`pack()` orders candidates per tier by

```
score_next = λ · utility(next) − (1 − λ) · max_jaccard(next, already_selected)
```

- **Similarity**: token-set Jaccard over lowercased alphanumeric runs of length ≥ 2
  (`token_set` / `jaccard` in `kernel/router/packer.py`). Deterministic; no embeddings.
- **λ = 0.7** (`MMR_LAMBDA`); `mmr_lambda=1.0` on `pack()` reproduces the pre-MMR
  packer exactly (tier-1 event-seq admission order, tier-3 utility order) — that escape
  hatch is what the benchmark compares against.
- **Ties** break on ascending `block_id`; selection is fully deterministic given
  identical inputs (asserted in `tests/test_packer.py`).
- **Budget admission is unchanged**: candidates are visited in MMR order and admitted
  while they fit; overflow still becomes tier-2 handles. Tier-1 output is re-sorted by
  `(source_event_seq, block_id)` afterwards, because the `Tier1Ledger` schema and its
  prefix-stability invariant fix that order — MMR changes *which* decisions are admitted,
  not the order they render in.
- Tier-3 shadow probes keep their deterministic `block_id` order and are always admitted
  first; the MMR penalty is seeded with the instruction and the admitted probes, then
  applied to the utility-ranked `others` pool.

Runtime stays within the performance gate: `compile_packet` over 1000 blocks measures
~61 ms mean on the dev box (five re-runs: 60.5-61.6 ms; `tests/test_perf.py`, budget
250 ms). The MMR loop switches
to utility order once the tier budget is saturated (no further candidate can be
admitted), which bounds the similarity work.

## 2. Packing benchmark — greedy vs λ-MMR, same budget

`evals/packing_bench.py` (offline, no GPU, no RNG) packs a labeled fixture of **60
blocks** at budget base 800 (tier1 200 / tier2 40 / tier3 360):

- 6 must-keep "answer key" decisions (unique facts),
- 16 near-duplicate decisions: 4 clusters × 4 one-word variants of the same decision
  (within-cluster pairwise Jaccard ≥ 0.6 by design, checked at run time),
- 8 unique decisions,
- 30 templated distractor facts/observations with low relevance.

Both arms call the real `pack()` with the same inputs; only `mmr_lambda` differs. Metrics
are computed over the selected blocks (tier1 + tier3 minus the instruction). Artifact:
`evals/reports/packing_bench_<ts>.json` (git-ignored; rerun the script to reproduce).

| arm | selected | avg pairwise Jaccard | must-keep coverage | packet tokens | tier1/tier3 tokens | tier2 handles |
|-----|---------:|---------------------:|-------------------:|--------------:|-------------------:|--------------:|
| greedy (λ=1.0, pre-MMR) | 27 | 0.2056 | 6/6 | 582 | 198/347 | 2 |
| λ-MMR (λ=0.7) | 28 | **0.1780** | 5/6 | 583 | 200/347 | 2 |

At roughly equal block count and budget, λ-MMR cuts the average pairwise redundancy of the
selected set by **13.4 %**. The trade-off is visible in the answer-key column: the pre-MMR
arm (event-seq admission) keeps all six must-keep decisions but spends tier-1 budget on
near-duplicate copies; λ-MMR keeps one copy per cluster and spends the freed budget on
unique decisions, at the cost of `answer-1` — whose 7443 fact is still present through the
surviving `dup-1-1` gateway block. The greedy arm is the pre-MMR packer exactly:
tier-1 decisions admit in event-seq order, tier-3 blocks in utility order.

The fixture checks its own design contract at run time (cluster similarity, relevance
spread, answer-key relevance) so the comparison cannot silently degrade into a relevance
accident. Caveat: the fixture is synthetic and deliberately shaped — 4 tight duplicate
clusters at an equal-utility plateau is the regime where MMR helps most; on real fabrics
the effect size will vary with the actual duplication rate.

## 3. Position probe — does WHERE the rule sits matter? (GPU)

`evals/position_probe.py` embeds one critical rule ("the gateway listens on port 7443;
always include 7443 in any port guidance") in a deterministic ~3.2k-token filler context
at three positions — HEAD (first ~10 %), MIDDLE (~50 %), TAIL (last ~10 %) — and asks 8
port-guidance probes per position. Scorer (crude, symmetric): success = the response
contains 7443 and no other 2–5 digit number; failure = missing 7443 or stating a
different port. n = 8 per position, temperature 0, `Qwen/Qwen2.5-1.5B-Instruct`
(enforce-eager), one session. Artifact: `evals/reports/position_probe_20261017_224320.json`.

| rule position | success | fail: missing 7443 | fail: different port |
|---------------|--------:|-------------------:|---------------------:|
| HEAD | 8/8 | 0 | 0 |
| MIDDLE | 8/8 | 0 | 0 |
| TAIL | 8/8 | 0 | 0 |

**Null result.** At 1.5B, on a 3.2k-token context with a single explicit rule, position
made no measurable difference (spot-checked responses restate the rule's core clause
verbatim — "the gateway listens on port 7443" — in several responses at all three
positions). This probe does **not** justify the U-curve policy as a measured
benefit — the recap and tail placement are shipped as low-cost, documented intent, not as
a proven effect. The result was not tuned: probes, positions, and scorer were fixed before
the run, and the null is reported as measured. A larger model or a longer, noisier context
might separate the positions; that experiment has not been run.

## 4. Placement policy (renderer)

`render_packet` order (documented in the module docstring): instruction → tier0 →
tier1 (packed order) → probes → observations → swap handles → `PROHIBITIONS` → output
contract → `KEY CONSTRAINTS` recap. The recap restates up to 2 of the most recent tier-1
decision/negative-knowledge blocks, each truncated to 64 whitespace tokens with an
ellipsis, skipping blocks whose full text or 12-word prohibition prefix is already rendered
under `PROHIBITIONS`.
`CF_RECAP=0` disables it; default on. The rationale is the attention U-curve (critical
constraints at the start and end of the prompt) — stated as intent, with the probe above
as the honest measurement of what we could and could not demonstrate.

## 5. How this compares to common practice

The default context builder in agent stacks is **top-k by similarity** (vector nearest
neighbours, BM25, or a reranker): score every candidate independently, take the k best,
concatenate in score order. That has two structural gaps this work closes:

- **No diversity control**: k near-duplicate blocks (repeated decisions, rephrased
  notes) all score high and all get injected; the budget is spent on restatements.
  λ-MMR adds a pairwise-redundancy term with deterministic Jaccard similarity — the
  MMR idea itself is standard IR practice (Carbonell & Goldstein, 1998), rarely applied
  inside an agent context budgeter.
- **No placement control**: similarity top-k has no notion of where a constraint lands
  in the prompt, and negative knowledge ("what failed") is usually mixed into the same
  ranked list, where it gets diluted by score order. The renderer now pins constraints
  at the head and tail and distills prohibitions + a recap there.

Both are deterministic, offline, and dependency-free. Caveat: this is an internal A/B of
our own two arms plus an honest null on placement — it is not a head-to-head benchmark
against any specific competitor implementation, and effect sizes depend on the fabric's
duplication rate.

## Caveats

- Synthetic fixtures only (packing bench and probe); real fabrics differ, especially in
  duplication rate and document length.
- The packing bench is one budget point (800) and one λ (0.7 vs 1.0); the MMR advantage
  is largest in the high-duplication, equal-utility regime the fixture deliberately
  creates.
- The position probe is n = 8 per position, one model (1.5B), one rule, one context
  length; its result is a null and is reported as such.
- The probe's scorer counts any other 2–5 digit number as a different-port claim (crude
  by design, symmetric across positions).
- Runtime cost of MMR is bounded and measured (perf gate), but similarity work grows with
  the candidate pool; the saturation short-circuit keeps the common large-fabric case
  linear-ish after the tier budget fills.

## Reproduce

```bash
uv run python -m evals.packing_bench                # offline; writes packing_bench_<ts>.json
wsl -d Ubuntu -- bash scripts/wsl_run_eval.sh evals.position_probe   # GPU (WSL)
uv run python -m pytest tests/test_packer.py tests/test_render.py tests/test_position_probe.py -q
```
