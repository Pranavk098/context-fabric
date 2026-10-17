# REFERENCES.md — Source Ledger

**Rule (per `docs/CHANGES-FROM-PROPOSAL.md`, C2):** every quantitative claim in the
specifications is either **(A)** sourced to a verifiable reference below, or **(B)** tagged
`[HYPOTHESIS]` and assigned to a measurement in `evals/`. There is no third category.

The original documents used opaque markers of the form `[cite: N]` with no bibliography.
Those numbers are retained in the prose only as *pointers into the named anchors below*;
they are not meaningful on their own. Where a claim previously relied on a `[cite: N]` we
could not resolve, it has been retagged `[HYPOTHESIS]`.

Last verified: 2026-10-16.

---

## A. Verified anchors

| Ref | Source | Supports |
| :--- | :--- | :--- |
| **R1** | Su et al., *ReFold: Training-Free Reversible Inter-Turn Context Folding for Long-Horizon Agents*, arXiv:2610.07863 (6 Oct 2026) | k-step chunked rendering preserves prefix cache; up to 2.5× token reduction; ~50% KV-cache memory reduction; best at k=3–5; reversible folding. Underpins Invariant 5 and `EPOCH_FOLD`. |
| **R2** | Lindenbauer et al., *The Complexity Trap: Simple Observation Masking Is as Efficient as LLM Summarization for Agent Context Management*, arXiv:2508.21433 (Aug 2025) | Simple observation masking halves cost vs. raw agent at equal/greater solve rate; summarization is not required. Baselines B5 and the "defer LLM summarizers" decision. |
| **R3** | *Don't Break the Cache: An Evaluation of Prompt Caching for Long-Horizon Agentic Tasks*, arXiv:2601.06007 | Provider prompt caching yields 41–80% cost reduction and 13–31% TTFT improvement **only on exact-prefix matches**. Directly motivates the strict prefix-stratification invariant. |
| **R4** | *Request Order Matters: Cache-History Sensitivity in Selective KV-Cache Reuse for Rolling Agents*, arXiv:2610.05833 (5 Oct 2026) | Rolling updates break exact prefix caching; motivates CacheBlend/LMCache non-prefix selective recomputation; summarizes vLLM PagedAttention and SGLang RadixAttention as prior art. |
| **R5** | Kwon et al., *PagedAttention / vLLM* ; Zheng et al., *SGLang / RadixAttention* | 256-token page-aligned KV management; the hardware target for the `.cfx` page aligner and `lmcache_driver`. |
| **R6** | Liu et al., *Lost in the Middle: How Language Models Use Long Contexts* (2024) | U-shaped attention placement; the Recency Salience Anchor. |
| **R7** | RFC 8785, *JSON Canonicalization Scheme (JCS)* | Deterministic canonical serialization for content hashing. |

## B. `[HYPOTHESIS]` claims — to be measured, not quoted

| Claim (as written in the docs) | Where | Measurement owner |
| :--- | :--- | :--- |
| "Multi-agent systems consume ~15× the tokens of a single chat interaction." | `Prod-Intro.md` §1 | `evals/harness.py` (B0 vs single-agent) — Stage B |
| "Naive observation masking dropped prefix-cache hit rates from 96.5% to 51% and increased net inference cost by 31%." | `Approach-Comp-Research.md` §1.1 (attributed to an "EFFICIENTAGENT, Sept 2026" source we could not locate) | Stage B spike (measure prefix-hit delta of B5) |
| "`LMCache`/`CacheBlend` provides 4.5×–7× speedups." | `Approach-Comp-Research.md` §3 Cat. D | Stage B spike on our hardware |
| ">88% KV-cache hit rate" and ">= 85% prefix hit rate" targets | `Prod-Intro.md`, `Build-Plan-Validation-Gate.md` Gate 2 | Stage B / Gate C |
| "Cold-start orientation tax: up to 80% of initial tokens." | `Approach-Comp-Research.md` §5 | Stage B harness |
| "Mem0 94.4% / Zep 90.2% LongMemEval; Zep 104 ms p50." | `Approach-Comp-Research.md` §3 Cat. A | Re-verify against primary sources before use in external material |
| MCP spec "2026-07-28"; A2A v1.0 (2026). | `Approach-Comp-Research.md` §2 Pillar 6 | Deferred with adapters (C4) |

## C. Named prior art referenced but not load-bearing for v1.0

LLMLingua / LLMLingua-2, RECOMP, MemGPT/Letta, Zep/Graphiti, Mem0, Cognee, LongMemEval,
Reflexion, ExpeL, RCR-Router, AgentPrune, Chain-of-Agents, AIOS, MemOS, Tree-sitter,
RepoMap, networkx PageRank. These are real works/system names used for positioning; no
quantitative claim from them should be quoted in external material without re-sourcing.

---

**Action for the author of the original docs:** provide the missing bibliography mapping
the numeric `[cite: N]` markers to actual entries, or accept the `[HYPOTHESIS]`
retagging above. The `[cite: N]` numbers currently carry no verifiable meaning.
