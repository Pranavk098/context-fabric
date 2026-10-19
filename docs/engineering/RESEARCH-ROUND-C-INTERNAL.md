# Research Round C — Internal Diagnosis + Option-A Feasibility

**Status: EXPLORATORY. Nothing in this file is a claim of record.** It is the internal
counterpart to `docs/research/RESEARCH-ROUND-C-EXTERNAL.md`. Part 1 reconstructs where the
experiment trail drifted from the original thesis; Part 2 is an exploratory feasibility probe
of "option A" (retrieve-then-compile) on the B4 v3 objectives, which are now a **dev set**.
Any headline a later run wants to make needs its own, separately pre-registered experiment
(B4 v4) on held-out objectives.

---

## Part 1 — where did we lose the plot

### 1.1 The thesis we started from

The three founding documents (`docs/product/Prod-Intro.md`, the CPP v0.1 architecture spec
and literature report at `C:\Users\<user>\.claude\jobs\53b8013a\tmp\context-*.pdf.txt`,
`docs/design/Arch-Spec.md`) made one central promise, with four sub-claims:

> **Central:** a framework-agnostic control plane compiles shared project truth into bounded,
> KV-aligned, auditable packets, so every agent wakes up with exactly what it needs — cheaper
> (fewer tokens), sharper (no Lost-in-the-Middle dilution), and auditable.

1. **Token claim** — stop paying to re-ingest the same context into every sub-agent (~15×
   multi-agent duplication tax → ~3,000-token packets).
2. **Quality claim** — accuracy *goes up* when irrelevant context is removed (small models
   included, via "model-tier arbitrage").
3. **Mechanism claim** — CoW sub-agent forking, Tier-0 cartridges, >85% KV-cache reuse, and
   zero-token cold boot make this structural rather than incidental.
4. **Portability/audit claim** — 0 frameworks abandoned; every packet replayable and signed.

`docs/decisions/ADR-0004` deliberately narrowed the beachhead to *local GPU / vLLM* and
`raw_llm`+`vllm` adapters — a scope cut, not a change of thesis.

### 1.2 Promise → experiment → what it actually measured

Each row names the promise, the experiment that was held up as its test, what that experiment
*actually* measured, the verdict already recorded in the repo, and whether it was a **fair
test** of the promise or a **different question**.

| # | Original promise | Experiment held up as the test | What it actually measured (baseline / task / model / metric) | Verdict | Fair test? |
|---|---|---|---|---|---|
| 1 | ~15× multi-agent duplication tax | Stage B audit F8 (`evals/audit_fixes.section_claim_15x`) | `multi_agent_tokens = n * single_tokens`; model client never called; no workload | **OVERSTATED** (N8) | **Different** — arithmetic identity, not a measured recovery |
| 2 | Compiled packets stop re-ingestion / cut tokens | PILOT, PILOT-MECH (`evals/pilot.py`) | real `httpx` 32k-token bundle; 6 short Q&A; 1.5B; **billed** prompt tokens (35.87× / 13.62×) | PASS on billed; **fails on uncached prefill** (raw 182 vs compile 11,857) | **Different** — billed-token ratio ≠ physical prefill; Q&A ≠ task success |
| 3 | Accuracy *improves* when irrelevant context is removed | B4 v2 (`evals/b4_divergent.py`) | 4-file ~8k slice **fits** the window; 12 identifier-lookup objectives; 1.5B; broadcast 35/36 vs prefix 29/36 vs compile 21/36 | **KILL** (B4-RESULT) | **Fair on fits-in-window**, but small model + lookup scoring, not task success |
| 4 | Model-tier arbitrage (small model + packet = frontier) | B6.4 (`evals/stage_b7.py`) | 0.5B vs 1.5B on trivial docstring checks; 86.7/93.3/93.3/80 | **UNDEMONSTRATED** (GAP-08, N21) | **Different** — saturated ceiling, no frontier model, no task |
| 5 | Zero-token cold boot via `.cfx` cartridge | B6.2 (`evals/audit_b6.section_coldboot`) | raw file listing vs hologram; 4 symbol→module lookups; 1.5B; 348 vs 3,146 tok, 100% vs 25% | **SUPPORTED** after information-equal fix (N6) | **Mostly fair** (post-fix) but n=4, synthetic listing, no open-ended task |
| 6 | CoW sub-agent forking, >85% KV reuse | Stage B2/B3/B5 (`evals/stage_b2/b3/b5.py`) | 8 children share a prompt prefix; measured `prefix_cache_hits`; **no CoW code in `kernel/`** | **CAVEAT** (D7): prefix-cache sharing, not CoW | **Different** — measures cache sharing, labels it CoW |
| 7 | Cross-agent concurrency win (20× KV, 8.6× throughput) | Stage B5 (16 concurrent) | unshared raw (`B0_divergent`) vs CPP-shared; KV% + req/s; no quality | 20.2× KV / 8.23× throughput **vs unshared only**; ~2× vs shared raw (N3/N4) | **Different** — strongest naive baseline is *unshared*; shared-prefix naive not measured |
| 8 | Quality parity under compression | Stage B4 (`evals/stage_b4.py`) | 12 facts + 24 distractors in a 572-token context; copy-a-value; 1.5B; 100% at 6.1× | **SUPPORTED but trivial** (GAP-22) | **Different** — extractive lookup, not reasoning or code task |
| 9 | Compile beats retrieval when the repo doesn't fit | B4 v3 (`evals/b4_v3.py`) | full `httpx` (~64.8k tok) vs 16,384 window; truncate/retrieval/compile; 20 blind objectives; 1.5B+0.5B; token scorer | **KILL** under fixed scorer (compile 7 vs retrieval 11 on 1.5B) | **Fair for THIS question**, but small models + identifier/value objectives |
| 10 | Real-repo ~17× compression | B6.1 (`evals/audit_b6.section_realrepo`) | `httpx` AST signature extraction, 64k → 3.8k tokens; no downstream use | **SUPPORTED** (16.83×) — signatures only, no bodies | **Different** — measures signature extraction, not usable agent context |
| 11 | Negative knowledge prevents repeated dead ends | B6.5 (`evals/audit_b6.section_negknowledge`) | 3 scenarios × 4 picks, bad option always first; 16.7% → 0% | **CAVEAT** (n=24, no repeats/significance) | **Different** — synthetic pick, not a real agent loop |
| 12 | Framework-agnostic / 0 frameworks abandoned | (none — no adapter suite exists) | ADR-0004 scoped to `raw_llm`+`vllm`; LangGraph/CrewAI/AutoGen/MCP/A2A deferred | **NOT BUILT** (D9) | **Different** — deferred, never tested |

### 1.3 The five drift axes (what the table rows share)

1. **Q&A lookup instead of agent task success.** Every quality experiment — B4, B6, PILOT,
   B4 v3 — scores a model's ability to *name an identifier or value* (substring/token match),
   never whether an agent completed a code change, fix, or audit. The "quality" the thesis
   promised (task success, no handoff loss) was never instrumented. Rows 2, 3, 5, 8, 9, 11.
2. **Synthetic vs real workloads.** The early cross-agent/fork/concurrency wins (rows 1, 6, 7)
   ran on synthetic, highly repetitive contexts with no correctness gate; the first *real*
   workload (PILOT, B4 v2/v3) is where the token and quality claims started to break.
3. **Small models.** 0.5B–1.5B Qwen models carry every quality result, yet the thesis targets
   *frontier-model replacement* and *small-model execution*. A 1.5B model misreads a correct
   packet (PILOT tester miss), flips answers on a leading tag (B4-EXPLORE), and is weakest
   exactly where the compile arm compresses hardest. No run has shown the effect at a frontier
   or 7B+ scale.
4. **Fits-in-window cases.** B4 v2's KILL is on a workload that *fits* the window, where
   vLLM prefix caching already captures the re-ingestion win. That falsifies a claim the
   thesis never made ("compiled beats prefix caching when it fits"); B4 v3 correctly moved to
   a non-fitting repo and the compile arm *still* lost on quality to plain retrieval.
5. **Billed vs uncached tokens.** The headline reductions (35.87×, 13.62×, 5.66×) are
   **billed** prompt tokens. When prefix caching is on, the raw shared prefix is prefilled
   once and re-read for pennies; the compiled packets, each role-distinct, pay a *cold*
   prefill and come out **worse on uncached (real prefill) tokens** (PILOT-MECH: 182 vs
   11,857). The "stop paying to re-ingest" claim survives only on billed-token accounting
   against a warm shared prefix, which is not the same thing.

**The load-bearing sentence:** the original thesis is about *multi-agent task quality per
unit of physical prefill*. The experiment trail measured *single-hop identifier recall at a
billed-token budget on small models*, and drifted on all five axes at once. The three
load-bearing results — B4 v2 (KILL), B4 v3 (compile < retrieval), PILOT-MECH (billed-only
win) — are individually honest but collectively answer a narrower question than the one the
product sold.

---

## Part 2 — option A feasibility ("retrieve then compile")

**Question:** on a repository that does not fit the window, does a hybrid that retrieves
full-body AST chunks by BM25 and then *compiles* them through the existing compiler beat plain
BM25 retrieval at equal budget — or match it at a lower budget?

**Fixture:** `encode/httpx` @ `b5addb64f0161ff6bfe94c124ef76f6a1fba5254`, the full `httpx/`
package (23 files, ~64.8k tokens), the B4 v3 objectives (20, frozen `evals/fixtures/b4v3_objectives.json`).
**Model:** `Qwen/Qwen2.5-1.5B-Instruct`, `temperature=0.0`, `max_tokens=96`, vLLM
`--max-model-len 16384 --enable-prefix-caching`. Harness: `evals/rx_hybrid.py` (new; does not
touch `evals/b4_v3.py`, `kernel/`, `sidecar/`, `protocol/`).

**Arms** (all 20 objectives × 1 repeat; each arm+budget carries a unique leading cache tag):

* **R** — BM25 over AST chunks (one per top-level class/function, full body, + module header),
  top chunks until the budget (the frozen B4 v3 retrieval arm).
* **H1** — the same retrieval, then the retrieved chunks are compiled through
  `compile_node_packet` with the shared Tier-0 cartridge + terse answer contract,
  `budget_base` = budget.
* **H2** — the same retrieval, then assembled **without the compiler**: repo-map header
  (cartridge hologram) + exact `from X import Y` import lines for the retrieved files +
  deduplicated chunks, all capped at budget.

The hybrid arms are retrieval-personalized per objective; they do **not** apply the compile
arm's file-ownership (role-divergence) map.

### 2.1 Offline (tokens + required-fact recall, fixed scorer)

Dry-run, no GPU. Receipt: `evals/reports/rx_hybrid_dry_qwen.json`. Prompt tokens are counted
with the Qwen tokenizer; recall is exact-token match under the **fixed** scorer
(leading/trailing `.`/`-`/`/` stripped, the B4 v3 correction).

| arm / budget | prompt tokens (20 reqs) | max per request | objectives w/ all facts | fact coverage |
|---|---:|---:|---:|---:|
| R @ 1k | 20,704 | 1,049 | 9 / 20 | 19 / 33 |
| H1 @ 1k | 19,324 | 1,103 | 9 / 20 | 17 / 33 |
| H2 @ 1k | 19,471 | 1,046 | 11 / 20 | 22 / 33 |
| R @ 2k | 40,670 | 2,048 | 15 / 20 | 27 / 33 |
| H1 @ 2k | 26,625 | 1,645 | 11 / 20 | 20 / 33 |
| H2 @ 2k | 35,509 | 2,040 | 14 / 20 | 25 / 33 |
| R @ 4k | 80,696 | 4,048 | 17 / 20 | 29 / 33 |
| H1 @ 4k | 40,851 | 2,683 | 12 / 20 | 23 / 33 |
| H2 @ 4k | 70,717 | 4,041 | 16 / 20 | 28 / 33 |

**Offline findings.** H1 is the cheapest arm but the *worst on recall at every budget*: the
existing compiler's tier structure (cartridge Tier-0 ≈ 545 tok + Tier-3 capped at 45% of the
budget + the instruction block) leaves the retrieved evidence starved — at 4k H1 delivers
~2.7k tokens of packet for a 4k budget and covers only 23/33 facts vs R's 29/33. H2 (the
no-compiler "retrieve + dedupe + imports + repo map" assembly) is a wash with R — a small
win at 1k (22 vs 19 facts), a small loss at 2k/4k (25/28 vs 27/29). **Offline, no hybrid
beats R at equal budget.**

### 2.2 GPU (2k / 4k, 1.5B, fixed + frozen scorer)

Receipt: `evals/reports/rx_hybrid_20261018_234149.json` (1.5B, 6 arm-configs × 20 objectives,
`temperature=0.0`, `max_tokens=96`, vLLM prefix caching on). Billed = prompt tokens; uncached
= billed − prefix-cache hits (real prefill); KV = peak cache usage.

| arm | budget | billed prompt tok | uncached tok | peak KV | frozen scorer | **fixed scorer** |
|---|---:|---:|---:|---:|---:|---:|
| R | 2k | 40,930 | 39,058 | 1.73% | 8 / 20 | **8 / 20** |
| H1 | 2k | 26,885 | 16,245 | 1.34% | 5 / 20 | **5 / 20** |
| H2 | 2k | 35,769 | 24,329 | 1.73% | 9 / 20 | **9 / 20** |
| R | 4k | 80,956 | 75,212 | 3.32% | 9 / 20 | **11 / 20** |
| H1 | 4k | 41,111 | 30,471 | 2.16% | 5 / 20 | **5 / 20** |
| H2 | 4k | 70,977 | 59,425 | 3.27% | 9 / 20 | **9 / 20** |

The fixed scorer changes only R @ 4k (9 → 11): the trailing-punctuation bug cost the
retrieval baseline its `5.0` answer, the same effect B4 v3 documented. H1 and H2 are unchanged.

### 2.3 Answer to the question

**Does any hybrid beat R at equal budget? No.** H1 (retrieve→compile) loses at both budgets
(5 vs 8 at 2k, 5 vs 11 at 4k, fixed scorer). H2 (retrieve + dedupe + imports + repo map, no
compiler) is a wash: +1 at 2k (9 vs 8), −2 at 4k (9 vs 11) — both inside the 1-request / ±5%
noise floor B4-EXPLORE measured on this model.

**Does any hybrid match R at a lower budget? No.** H2 @ 2k (9) does not reach R @ 4k (11);
H1 is strictly dominated at every budget.

**By how much:** H1 is −3 to −6 requests vs R (fixed scorer); H2 is +1/−2. Neither is a
meaningful win. The one thing H2 does buy is prefill, not quality: its shared repo-map +
imports prefix yields ~6× more prefix-cache hits than R at 2k (11,440 vs 1,872) and ~35%
lower uncached prefill — but it answers no more questions.

### 2.4 Recommendation

**No-go for a pre-registered B4 v4 of "retrieve then compile" as specified.** The offline
recall already predicted the ordering (H1 < R, H2 ≈ R) and the GPU quality confirms it, on
both scorers. Two concrete reasons: (1) the existing compiler is the wrong tool to wrap
around retrieved full-body chunks — its 4-tier budget (shared Tier-0 cartridge ≈ 545 tok +
Tier-3 capped at 45% + instruction) leaves the evidence starved, and its MMR/utility
re-ranking does not help a 1.5B model, so H1 delivers ~half the evidence and fewer answers;
(2) the only arm with a real token/prefill benefit is the *no-compiler* H2, and that benefit
does not translate into quality — H2 is a wash with R.

If option A is pursued at all, pursue the **H2 direction** (cheap deterministic glue: repo-map
header + exact import lines + dedupe, no compiler tiers), and pre-register two things the
current run cannot decide: whether the prefill win holds at a model size that can actually do
the task, and whether it holds on *task success* rather than identifier recall. On this 8 GB
GPU the 3B model cannot serve a 16,384-token window (B4 v3 §3), so that decisive experiment
is out of reach here.

