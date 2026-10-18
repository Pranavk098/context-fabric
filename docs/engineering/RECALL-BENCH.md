# RECALL-BENCH — lexical vs BM25 vs embeddings vs hybrid

- **Date:** 2026-10-17. **Owner scope:** `kernel/router/recall.py`, `kernel/router/embeddings.py`,
  `evals/recall_bench.py`. **Artifact:** `evals/reports/recall_bench_<ts>.json` (per-query rows +
  aggregates; this write-up uses `recall_bench_20261018T061752Z.json`, regenerated post-audit on
  the frozen code). The previous artifacts (audited) were deleted; the fresh run is byte-identical
  to them to full float precision — the hybrid `idf` block-id fix cannot trigger on this corpus,
  which contains no block id `idf`.
- **Question:** the Stage-4 closure seeds used naive lexical overlap (`|q∩d|/|q|`). Does replacing
  it with Okapi BM25 — and optionally with local embeddings — measurably improve retrieval?

## Method

- Corpus = candidate block set (as in `compile_packet`: allowed blocks after hard policy).
  Scores are candidate-set relative by construction (IDF depends on the corpus).
- Tokenization (documented in `kernel/router/recall.py`): lowercase, split on non-alphanumerics
  (Unicode-aware), keep tokens len>=2. No stemming. The `lexical` baseline reimplements the OLD
  tokenizer (`[a-z0-9_]+`) locally so the baseline is frozen at its shipped behavior.
- BM25: k1=1.2, b=0.75; positive RSJ IDF `ln(1 + (N−df+0.5)/(df+0.5))`, so a block scores exactly
  0.0 iff no query token occurs in it — preserving the old seed semantics (`score > 0` ⇔ lexical hit).
- Modes: `lexical` (old overlap), `bm25`, `embeddings` (cosine, BAAI/bge-small-en-v1.5 via
  fastembed), `hybrid` (0.5·max-normalized BM25 + 0.5·max-normalized cosine, negatives clamped;
  `kernel/router/embeddings.py`). Mode selection in the compiler is `CF_RECALL=bm25|hybrid|embeddings`
  (default `bm25`); missing extra degrades to BM25.
- Metrics (macro-averaged over queries): precision@5, recall@10, MRR, top-1 accuracy. Ranking ties
  break by `block_id` (deterministic). No RNG anywhere; `uv run --extra embeddings python -m evals.recall_bench`.

## Labeled set (synthetic — labels assigned by construction, not by human judgment)

120 blocks, 20 queries, 2 labeled-relevant blocks per query (40 labels). 20 topics x 6 notes:

| slot | count | construction | role |
|---|---|---|---|
| rel_a/rel_b | 2/topic | focused notes containing the query's content tokens (32/34 contain every query token; two miss one) | labeled relevant |
| dup_a/dup_b | 2/topic | near-duplicates sharing most query tokens (28/34 share exactly \|q\|−1; the rest share one token or, for one block, all) but describing another subsystem | lure partial-match rankers |
| neg_a/neg_b | 2/topic | keyword-stuffed digest lines containing the **full query string** in a ~30–40-token low-density note | lure naive overlap |
| paraphrase | 3 queries | query and its relevant notes share no content token (two of six blocks share a single stopword) | semantically hard for lexical modes |

Labels are fixed by which slot generated the block. This is a synthetic set: it does not measure
real-world recall, it measures robustness to two documented lexical failure modes (keyword stuffing,
partial-token near-duplicates) plus paraphrase cases.

## Results (20 queries, identical corpus for all modes)

| mode | P@5 | R@10 | MRR | top-1 |
|---|---|---|---|---|
| lexical (old) | 0.340 | 0.875 | 0.289 | 0.000 |
| bm25 | 0.340 | 0.875 | **0.859** | **0.850** |
| embeddings | **0.380** | **0.975** | **0.889** | **0.850** |
| hybrid | **0.380** | 0.950 | 0.882 | **0.850** |

- **The BM25 upgrade is the decisive win vs the shipped baseline:** top-1 0.00 → 0.85, MRR
  0.289 → 0.859. The old overlap ties the keyword-stuffed negatives (score 1.0) with the
  relevant notes on 15/17 keyword topics (on the other two the negative scores strictly
  higher) and never ranks a relevant block first; BM25's length normalization + saturated TF
  separates them. On the 17 keyword-findable topics BM25 puts a relevant block at rank 1 in 17/17.
- **Embeddings/hybrid vs BM25 on this set:** embeddings-only edges hybrid (R@10 0.975 vs 0.950,
  MRR 0.889 vs 0.882 — a one-query difference) and both beat BM25 on P@5/R@10. Neither semantic
  mode fixed top-1 on the three paraphrase queries (best first-relevant ranks: 3, 9, 3 vs BM25's
  44, 45, 7). The blocker for all modes is the keyword-stuffed negative: it embeds the **verbatim
  query string**, so it ranks 1–2 for BM25 *and* for the embedding model (a query inside the
  passage has high cosine); near-duplicate stubs also compete in the tail.
- **Honest answer to "does hybrid beat embeddings-only?":** no — not on this set. Hybrid beats
  BM25-only on every metric (P@5 +0.04, R@10 +0.075, MRR +0.023) but trails embeddings-only
  slightly. We do not claim otherwise.

## vs how competitors do it

The common competitor design in this space is embedding-vector search over a memory/graph store
(see `docs/research/SHARED-MEMORY-PRIOR-ART.md` §3: Mem0/cognee/Graphiti-style retrieve layers),
and GroupMemBench (arXiv 2605.14498) found plain BM25 matches most "memory" systems — so both
baselines matter. On this synthetic set: hybrid > BM25-only and hybrid ≈ (slightly below)
embeddings-only. **Caveats:** the set is small (20 queries; one query = 0.05), the two stuffed
negatives per topic give length-normalizing BM25 a structural target (on keyword queries it wins
where the old overlap tied and lost), the three paraphrase queries structurally favor embeddings,
and the labels are construction-fixed rather than human-annotated. Note the cross-cutting finding:
verbatim keyword stuffing beats BM25 *and* the small embedding model — pure vector search is not a
defense against stuffing either; closure seeds only need `score > 0`, so ranking order matters less
there than for top-k prompt assembly. On paraphrase-heavy real data embeddings-only can win outright;
on keyword/API-heavy notes BM25 (no model download, exact reproducibility) stays competitive. The
0.5/0.5 hybrid is the hedge, not a claimed dominance.

## Determinism and reproducibility

- BM25/lexical/hybrid combination math: pure Python, fixed parameter constants, query terms summed
  in sorted order — byte-identical scores across runs (verified by re-running the bench: identical
  aggregates).
- Embeddings: CPU ONNX inference, deterministic for a pinned model artifact. Resolved on this run:
  fastembed 0.9.0, `BAAI/bge-small-en-v1.5` → `Qdrant/bge-small-en-v1.5-onnx-q`, snapshot
  `aa8f8b060edb00e03bfdd08813a2949946c8ba55`, `model_optimized.onnx` sha256
  `51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431` (66,465,124 bytes). Download
  cache is fastembed's default (`%TEMP%\fastembed_cache` on Windows) — CI should pre-warm or pin it.
- The benchmark contains no RNG; block order, ids, and tie-breaks are fixed strings.

## Caveats

- Synthetic corpus; aggregates depend on construction choices (stuffed-negative density, near-dup
  overlap). Treat as a regression gate, not an absolute quality claim.
- No stemming in the shared tokenizer (documented trade-off: zero dependencies, deterministic).
- Candidate-set-relative IDF means scores are not comparable across different candidate sets.
- Embedding tests are skip-guarded when the extra is absent; the benchmark drops those modes.
