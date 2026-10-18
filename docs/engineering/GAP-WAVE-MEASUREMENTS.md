# GAP-WAVE-MEASUREMENTS — independent verification of the gap-closure wave's claims

- **Date:** 2026-10-17. **Auditor role:** adversarial recomputation (desk audit).
- **Method:** read every measurement script, re-derived every number from the stored
  artifacts with fresh code (no reuse of the bench's own aggregation path where a second
  implementation was cheap), re-scored stored model responses with the shipped scorers,
  re-ran the deterministic offline benches end to end, re-ran `tests/test_perf.py` five
  times, and verified on-disk model-artifact pins and external citations.
- **Concurrent-review interaction (important):** a separate adversarial code review
  (`docs/engineering/GAP-WAVE-REVIEW.md`) landed in the working tree *during* this audit
  and changed measurement-relevant code: the packer's λ=1.0 escape hatch (GW-03), the
  renderer's recap skip (GW-04), the hybrid score selection (GW-05), and recall/compiler
  comments (GW-09). This document reports the **post-fix tree**. Where the wave's original
  numbers were invalidated by those fixes, that is stated explicitly; the GW-03 fix and
  the corrected packing numbers were independently re-verified here (not trusted from the
  other review).
- **No GPU re-runs were performed.** Every stored count re-derived exactly, so a vLLM
  re-run would only re-roll sampling noise, not adjudicate a defect. GPU process claims
  that cannot be checked from an artifact are listed under *Residual repro gaps*.
- **Gates after the edits below:** `ruff check`, `ruff format --check`, `mypy protocol
  evals kernel sidecar`, and `pytest -q` all green (173 passed).

## Verdicts per claim

| # | Claim | Verdict |
|---|---|---|
| 1 | Recall benchmark (`evals/recall_bench.py`, `RECALL-BENCH.md`) | **RECOMPUTED — exact.** All four modes' aggregates reproduce to full float precision; independent re-score (including embeddings/hybrid on the pinned local model) reproduces every aggregate; two artifacts byte-identical except `generated_at_utc`; old-overlap baseline fidelity confirmed against the pre-wave implementation; model pin verified on disk. Four *construction* statements were slightly stronger than the corpus (corrected, §Corrections). Unaffected by the concurrent fixes. |
| 2 | Packing benchmark (`evals/packing_bench.py`, `PACKING-QUALITY.md`) | **SUPERSEDED — original control was the wrong algorithm (concurrently found, GW-03); corrected numbers re-verified exactly.** The on-disk artifact `packing_bench_20261017_224744.json` is internally consistent with the *pre-fix* packer, but its "greedy (λ=1.0)" arm measured utility-ordered MMR at λ=1.0, not the pre-MMR event-seq admission the doc claimed. The concurrent review fixed `pack()` (λ=1.0 now reproduces legacy admission — independently re-verified here) and rewrote the doc table. Fresh re-run matches the corrected doc exactly; a new artifact `packing_bench_20261017_231341.json` was generated. **The original "MMR at better coverage (5/6 vs 3/6)" headline is falsified**: against the correct control, MMR is 5/6 vs greedy's 6/6. |
| 3 | Position probe (`evals/position_probe.py`, `PACKING-QUALITY.md` §3) | **RECOMPUTED — exact (8/8/8).** Prompts differ only in rule position (identical line multisets, byte-identical after removing the rule line, equal token counts); same scorer both; every stored row re-scores identically; stored responses consistent. Doc's "quote the rule verbatim" wording corrected (responses restate the core clause, not the full rule). |
| 4 | Refreshed demo artifacts (`DEMO.md`) | **RECOMPUTED — exact.** All fleet/smoke/negative counts, ratios, cache deltas, pilot counts, trap sets (`queue, metrics` at 3B), write seqs (27/28), `response_text`/`prompt_sha256` semantics, and the smoke-first run order verified; v1/v2 re-scoring has **zero** mismatches. Supersession notes are true. One wording fix ("minutes" → "14 s"); one new reproducibility caveat from the concurrent GW-04 renderer fix (see *Residual repro gaps*). |
| 5 | Perf numbers (`tests/test_perf.py`, `PERFORMANCE.md`) | **MISMATCH — stale doc (fixed).** Doc claimed 3.3 / 6.4 / 23.3 ms; five fresh sessions measure **4.9-5.1 / 13.7-14.1 / 60.5-61.6 ms**. The doc's numbers predate the λ-MMR selection pass added in this wave (HEAD packer had no MMR). `PACKING-QUALITY.md`'s concurrent "~67 ms" was a load-inflated single run and was reconciled to ~61 ms (five sessions). Doc table, derived ratios ("10x" → ~4x; "one tenth" → ~a fifth), the KV-spike range, and the stale `test_perf.py` docstring ratio were corrected. |
| 6 | Cross-doc consistency | **Two real defects found (fixed).** `ENGINEERING-LOG.md` pointed at a nonexistent `stage_b5_20260316_153211.json` (actual: `20261016`); `AGENT-HINTS.md` cited `SOTA-AND-THREATS.md` §3.5 for the hint-channel material (actual: §3.2). `demo_negative.json` in DEMO.md is a CLI `--out` example, not a path claim. All other "saved to" artifact paths resolve. |

## Every number re-derived

Recall benchmark (artifact `recall_bench_20261018T053623Z.json`; second artifact
`...T053742Z.json` identical except its timestamp):

| Number | Claimed | Recomputed | Source |
|---|---|---|---|
| corpus 120 blocks / 20 queries / 40 labels / 3 paraphrase | yes | exact | artifact `corpus`; rebuilt `build_corpus()` |
| lexical P@5/R@10/MRR/top1 | 0.340 / 0.875 / 0.289 / 0.000 | 0.34 / 0.875 / 0.28948 / 0.0 | per-query rows re-aggregated; independent re-score |
| bm25 | 0.340 / 0.875 / 0.859 / 0.850 | 0.34 / 0.875 / 0.85938 / 0.85 | same |
| embeddings | 0.380 / 0.975 / 0.889 / 0.850 | 0.38 / 0.975 / 0.88889 / 0.85 | same + full local re-score on pinned model |
| hybrid | 0.380 / 0.950 / 0.882 / 0.850 | 0.38 / 0.95 / 0.88214 / 0.85 | same |
| hybrid beats BM25 by +0.04 / +0.075 / +0.023 | yes | exact (0.04 / 0.075 / 0.02274) | aggregates |
| bm25 rank-1 on keyword topics 17/17 | yes | 17/17 | per-query `first_relevant_rank` |
| paraphrase first-relevant ranks bm25 44/45/7; emb 3/9/3 | yes | exact (hybrid 4/17/3; lexical 44/59/6) | per-query rows |
| fastembed 0.9.0; snapshot `aa8f8b06…`; `model_optimized.onnx` sha256 `51f1bd0a…`; 66,465,124 B | yes | exact | uv.lock; `%TEMP%\fastembed_cache` on disk |
| old-overlap baseline == pre-wave implementation | "frozen at shipped behavior" | 0/2,400 (query, block) score mismatches vs HEAD `recall.py` | differential re-implementation |

Packing benchmark — **original artifact (pre-fix packer)**, `packing_bench_20261017_224744.json`:

| Number | Doc claim at the time | Recomputed | Verdict |
|---|---|---|---|
| greedy 28 selected / 0.2126 / 3/6 / 576 / 193-347 / 2 | yes | exact vs the then-current code | internally consistent, but the arm was **not** the pre-MMR greedy (GW-03) |
| MMR 28 / 0.1780 / 5/6 / 583 / 200-347 / 2 | yes | exact | valid (MMR path unchanged) |

Packing benchmark — **corrected** (fresh re-run with the fixed packer, artifact
`packing_bench_20261017_231341.json`):

| Number | Corrected doc claim | Recomputed | Source |
|---|---|---|---|
| greedy (λ=1.0 = pre-MMR admission) 27 / 0.2056 / 6/6 / 582 / 198-347 / 2 | yes | 27 / 0.205616 / 1.0 / 582 / 198-347 / 2 | fresh `pack()` run; independent Jaccard |
| λ-MMR 28 / 0.1780 / 5/6 / 583 / 200-347 / 2 | yes | 28 / 0.177986 / 0.833333 / 583 / 200-347 / 2 | same |
| redundancy −13.4% | yes | −13.44% | recomputed |
| `answer-1` dropped by MMR; its 7443 fact survives via `dup-1-1` | yes | `answer-1` absent from MMR selected, `dup-1-1` present | `selected_ids` |
| `pack(λ=1.0)` reproduces pre-MMR tier-1 admission exactly | asserted by GW-03 test | independent re-implementation: tier-1 ids identical (`answer-1..6, dup-1-1..4, dup-2-1..4, dup-4-1`) | differential check |
| fixture contract: cluster min-Jaccard ≥0.6; relevance spread ≤0.35; must-keep ≥0.2 | asserted | 0.600/0.667/0.727/0.750; 0.083; all 0.333 | `_fixture_check` + manual |

Position probe (artifact `position_probe_20261017_224320.json`):

| Number | Claimed | Recomputed | Source |
|---|---|---|---|
| HEAD/MIDDLE/TAIL 8/8, no other-number failures | yes | exact; every row re-scores identically | stored rows + `score_response` |
| identical content across positions | yes | line multisets equal; byte-identical minus rule line; rule at index 0/15/30 | `contexts()` |
| context 3220 tokens, equal across positions | yes | 3220/3220/3220 (o200k) | `count_tokens` |
| 8 probes per position, same order, one model, temp 0 | yes | probes identical; model string matches | artifact rows; `harness.chat` payload |

Demo receipts:

| Number | Claimed | Recomputed | Source |
|---|---|---|---|
| fleet broadcast/compile prompt tokens 44854 / 14170; 3.17× | yes | 44854 / 14170 = 3.1655 | `demo_fleet_20261017_224442.json` records |
| both arms 6/6; completions 188/139; wall 2.83/1.94 s; mean 471/323 ms | yes | exact | same |
| warm cache 44816/44854 (99.9%), 14128/14170 (99.7%); KV 0.0605/0.0201 | yes | exact | `cache.per_arm` |
| smoke (cold, 14 s earlier): broadcast 37280/44854 (83.1%); compile 0/14170 | yes | 83.11%; 0 | `demo_fleet_smoke_20261017_224428.json` |
| blob 6687 o200k tokens; fixture 26 blocks (3 dec / 3 fact / 1 obs / 2 NK / 1 draft + 16 notes) | yes | 6687; 26 with matching kinds | rebuilt fixture + `broadcast_text` |
| 1.5B pilot search 0/2, queue 0/2, cache 2/2, metrics 0/2, vector 0/2 → trap `cache` | yes | exact; frozen-by-rule = [cache] | `demo_negative_20261017_224452.json` |
| 1.5B control v1 2/2 v2 2/2, treated v1 2/2 v2 1/2; tokens 4884/5072 | yes | exact; all 8 rows re-score identically | records + `is_bad_answer(_v2)` |
| 1.5B write seq 27 via POST | yes | seq 27, `blk_43ad7cf08bcc0814` | write record |
| 3B pilot queue 2/2, metrics 2/2, cache 0/2 → traps `queue, metrics` | yes | exact | `demo_negative_3b_20261017_224608.json` |
| 3B per-trap v1/v2 cells; pooled 4/4 & 2/4 both arms; tokens 9782/10454 | yes | exact; 16 rows re-score identically | records |
| 3B writes seq 27/28 | yes | seq 27, 28 (ids match DEMO-REVIEW's quoted hashes) | write records |
| `response_text` = first 500 chars; `prompt_sha256` = client-sent messages | yes | all 60+ rows: `response_text == response[:500]`; sha recomputed from client messages for every chat row (incl. broadcast blob + objective) | records + `content_hash` |
| manual-adjudication quotes (1.5B "instead of adding another caching layer like Redis or Memcached" → v2 false positive; "**Redis Cache**" heading → false negative; 3B "SQLite-backed queue … avoids the pitfalls associated with Redis") | yes | quotes present in stored responses; v2 flags match | artifact responses |

Perf (`tests/test_perf.py`, five sessions):

| Number | Doc claim | Re-run | Verdict |
|---|---|---|---|
| compile 50 blocks | 3.3 ms | 4.88-5.06 ms | stale → fixed to 5.0 ms |
| compile 200 blocks | 6.4 ms | 13.66-14.09 ms | stale → fixed to 13.7 ms |
| compile 1000 blocks (mean of 30) | 23.3 ms | 60.46 / 60.71 / 61.19 / 61.26 / 61.62 ms | stale → fixed to 61 ms |
| render 1000-block packet (mean of 5) | 0.01 ms | 0.01 ms | exact |
| rerun variance <3% | — | 1.9% (1000-block, five sessions) | holds |
| budget gate 250/50 ms | pass | pass | holds |

## Label leakage and construction honesty

Recall bench:

- The `lexical` baseline is a faithful freeze of the pre-wave shipped scorer: the bench's
  local `[a-z0-9_]+` re-implementation produced **0 mismatches** against HEAD
  `kernel/router/recall.py` over all 20 × 120 (query, block) pairs. No leakage from the
  new tokenizer into the old baseline.
- Labels are true by construction as claimed: all 40 negative blocks embed the full query
  string verbatim; 32/34 relevant blocks contain every old-tokenizer query token; 28/34
  near-duplicates share exactly |q|−1 query tokens. Deviations are minor and now stated
  in the doc: `u-curve rel_a` misses `placement`, `sqlite-journal rel_b` misses `sqlite`;
  `packet-hash dup_b` shares all 3; three dup blocks share 1. Paraphrase blocks are
  "no content-token overlap" rather than literally zero-token: two of six share a single
  stopword (`to`, `when`) — not a ranking signal that changes the outcome (BM25 still
  needs ranks 44/45/7).
- The old-overlap tie story is accurate in outcome (top-1 0.000 for all 20 queries; 19/20
  top-1 slots are `neg_a`) with one precision fix applied: the relevant notes score 1.0
  (tying the negatives) on 15/17 keyword topics; on the other two the negative scores
  strictly higher. Either way the shipped baseline never ranks a relevant block first.
- Negative-note length: 30–40 o200k tokens (mean 32.5), so "~40-token" was corrected to
  "~30–40-token".
- Metric implementations verified: P@5 = |rel∩top5|/5, R@10 = |rel∩top10|/|rel|,
  MRR = 1/rank, top-1 exact; ties break on ascending `block_id`; no RNG.

Packing bench:

- Both arms call the real `kernel.router.packer.pack()` (no reimplementation). After the
  concurrent GW-03 fix, λ=1.0 reproduces the pre-MMR tier-1 admission order exactly
  (independently re-verified against a from-scratch re-implementation), so the control is
  now the algorithm the doc claims.
- The must-keep set is fixture-defined (`_MUST_KEEP` → `answer-1..6`) and is not derived
  from the packer or its objective; coverage is a membership count over those ids, so the
  MMR similarity function cannot influence it. The redundancy metric does share the
  packer's `token_set`/`jaccard` — that is stated in the doc and is the natural measure
  for the selector's own similarity term, not a leak into the outcome variables.
- The single-budget-point caveat is adequately stated (PACKING-QUALITY.md §Caveats:
  "one budget point (800) and one λ (0.7 vs 1.0); the MMR advantage is largest in the
  high-duplication, equal-utility regime the fixture deliberately creates").
- The corrected doc also states the coverage trade-off honestly (`answer-1` dropped,
  fact preserved through `dup-1-1`), which the original doc did not need to because its
  control was wrong.

Position probe:

- Fairness verified end to end: same 30 filler sections + same rule, rule position the
  only change; equal token counts asserted in-script; same 8 probes; one scorer applied
  to every response; raw responses stored and every stored verdict reproduces.
- The U-curve rationale is explicitly documented as intent, not a measured benefit
  ("does **not** justify the U-curve policy as a measured benefit"), which is exactly what
  the null supports.

Demo receipts:

- `prompt_sha256` semantics documented accurately: it covers the client-sent messages
  only; in compile mode the injected packet is rendered server-side and excluded (DEMO.md
  §Reading the receipt and the receipt notes both say so). Recomputed for every chat row.
- v2 is described honestly as an exploratory post-observation instrument fix, with both
  scorers reported; re-scoring reproduces every stored verdict, and the two documented
  failure modes are demonstrable in the stored responses.

## Corrections made (docs only)

| File | Change | Why |
|---|---|---|
| `docs/engineering/PERFORMANCE.md` | Measured table → 5.0 / 13.7 / 61 ms; note that the pre-MMR numbers were 3.3/6.4/23.3; "roughly 10x" → "roughly 4x"; KV-spike scale → ~160–670 ms (mean ~300 ms, cited to `kv_spike_20261016_142753.json`); "one tenth" → "a fifth" | Stale numbers predating this wave's λ-MMR pass; the original 240–470 ms range is not reproducible from the named source (actual 158.4–668.3 ms) |
| `docs/engineering/PACKING-QUALITY.md` | "~67 ms" → "~61 ms (five re-runs: 60.5-61.6 ms)" | The concurrent review's single 66.7 ms run was load-inflated (measured during the concurrent audit); five sessions agree on ~61 |
| `tests/test_perf.py` | Docstring "roughly 5-7x" → "roughly 4x" | Same staleness; comment-only, bounds untouched |
| `docs/engineering/RECALL-BENCH.md` | Construction table: dup claim now "28/34 share exactly \|q\|−1 …"; rel claim notes the two one-token misses; neg note "~30–40-token"; paraphrase claim "no content token (two of six blocks share a single stopword)"; tie sentence now "15/17 keyword topics" | Statements were stronger than the corpus; outcome-level claims unchanged |
| `docs/engineering/PACKING-QUALITY.md` §3 | "quote the rule verbatim" → "restate the rule's core clause verbatim … in several responses" | Full rule never quoted verbatim; the core clause is |
| `docs/engineering/DEMO.md` | "minutes earlier" → "14 s earlier" (smoke→fleet gap) | Internal contradiction with the same doc's "14 s earlier" and the artifacts |
| `docs/engineering/AGENT-HINTS.md` | §3.5 → §3.2 for CacheScout/Continuum | §3.5 is the LMCache substrate item; §3.2 is "Ride the app→engine hint channel" |
| `docs/engineering/ENGINEERING-LOG.md` | `stage_b5_20260316_153211.json` → `stage_b5_20261016_153211.json` | Path did not exist (typo) |
| `docs/engineering/DEMO-REVIEW.md` | One-line supersession banner pointing at the `_2244xx` set | Its 3.39×/old-artifact numbers are historical; without a banner they read as current |

Corrections made concurrently by `GAP-WAVE-REVIEW.md` (verified here, not re-litigated):
`PACKING-QUALITY.md` §2 table/§1/§4 rewritten for the corrected control (13.4 %, 27 vs
28 selected, 5/6 vs 6/6 coverage); `kernel/router/packer.py` λ=1.0 legacy admission;
`sidecar/render.py` recap 12-word-prefix skip; `kernel/router/embeddings.py` hybrid
block-id selection; `sidecar/mcp.py` stdio hardening; comment fixes in
`kernel/router/recall.py`/`compiler.py`.

Not edited (out of mandate): `README.md` (clean), `CHANGELOG.md` (retains the historical
"3.39×" line describing the previous Track B commit — superseded by DEMO.md's 3.17× for
the current set; flag for the next changelog pass), `IMPLEMENTATION-STATUS.md`.

## External citations spot-checked

- vLLM RFC #52113 exists, is **Open**, and matches the described agent-hint concepts
  (verified on GitHub; the "not shipped" status is the issue's own proposal state).
- GroupMemBench arXiv 2605.14498 exists; its abstract supports "a simple BM25 baseline
  matches or exceeds most agent memory systems" as cited in RECALL-BENCH.md.

## Residual repro gaps

1. **No GPU re-run.** Position-probe and demo numbers are recomputed from stored
   artifacts; the serving-side process claims (vLLM 0.31.0, `--enforce-eager`, one
   session, temperature 0) are attested by receipts/docs and consistent with the code,
   but not re-executed here. Nothing in the recomputation suggested a defect, so a
   re-run would not change any verdict.
2. **Packing artifact supersession.** `packing_bench_20261017_224744.json` (pre-fix
   control) remains on disk next to the regenerated `packing_bench_20261017_231341.json`
   (corrected control). The doc no longer cites a specific timestamp, but a reader who
   opens the older file will see the wrong-control numbers — delete or annotate it when
   convenient.
3. **Demo receipts predate the GW-04 renderer fix.** The treated prompts were rendered
   before the recap learned to skip 12-word prohibition prefixes, so the treated
   dead-end block was rendered twice (prohibition + recap). A fresh `cfx demo` run would
   produce shorter treated prompts (different `prompt_tokens` than 5072/10454); the
   receipt-bound v1/v2 counts and ratios remain internally consistent. DEMO.md does not
   state this provenance gap.
4. **Deleted pre-v2 receipts.** `_211536`/`_212021` negative runs are unrecoverable
   (never committed; `evals/reports/` is gitignored). The v2 pre-registration narrative
   in DEMO.md/demo.py remains self-attested — the same F3/F4 provenance gaps the earlier
   DEMO-REVIEW already recorded. The new smoke-first run order *does* close the earlier
   warm-cache provenance gap (F2).
5. **Qwen tokenizer figures.** The 7449-Qwen-token blob claim was not re-derived (no
   Qwen tokenizer locally); the o200k figure (6687) is verified.
6. **"below clock resolution" render cells** for 50/200 blocks are not measured by
   `test_perf.py` (it renders only the 1000-block packet); unverifiable from the test.
7. **Process claims** ("probes/positions/scorer fixed before the run", "hybrid is the
   hedge, not a claimed dominance") cannot be timestamp-verified from artifacts; the
   artifacts and code are consistent with them.
8. **Recall artifacts live only outside git** (`evals/reports/` gitignored): future
   audits of this set depend on the files surviving on this machine.

## Headline verdicts

- **"BM25 decisively fixes recall" — supported, scoped.** On this synthetic set the
  shipped naive overlap never ranked a relevant block first (top-1 0.000) while BM25
  reaches top-1 0.85 / MRR 0.859 and 17/17 rank-1 on keyword topics. The honest limits
  are in the doc and verified: P@5/R@10 are unchanged vs the old baseline, the three
  paraphrase queries remain unsolved (best ranks 44/45/7), and the corpus is deliberately
  shaped around the old baseline's two documented failure modes. "Decisively fixes" is
  fair for *ranking under keyword stuffing*, not for precision/recall or semantic recall.
- **"MMR cuts redundancy 16% at better coverage" — NOT supported as stated (falsified by
  the corrected control).** The original benchmark compared λ-MMR against a λ=1.0 arm
  that still used MMR's utility ordering, not the pre-MMR packer; the concurrent GW-03
  fix corrected the control and the doc. Against the correct control: redundancy
  **−13.4 %** (0.2056 → 0.1780) at **worse** answer-key coverage (**5/6 vs 6/6**), with
  the dropped key's fact still present through a surviving near-duplicate. Redundancy
  reduction is real; "better coverage" and the 16 % figure were artifacts of the wrong
  control. The corrected PACKING-QUALITY.md states the trade-off honestly; scope remains
  one budget point, one λ, deliberately high-duplication fixture.
- **"Position probe null" — supported as measured.** 8/8/8 with identical content, equal
  token counts, one symmetric scorer; reported as a null and explicitly *not* used to
  justify the U-curve policy.
