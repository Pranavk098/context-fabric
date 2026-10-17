# Context Fabric (CPP v1.0) — SOTA & Threats

**Date:** 2026-10-16 · **Scope:** 2026 state of the art relevant to the "semantic MMU / context compiler" thesis.
**Method:** targeted web/arXiv survey across five tracks. Each source = key claim + **SUPPORTS** / **THREATENS** verdict against Context Fabric's claims (tiny L1 register ~3k dynamic tokens, prefix-cache-aligned auditable packets, CoW sub-agent fork over immutable Tier 0 + append-only Tier 1, k-step micro-epochs, first-class `negative_knowledge`, deterministic core, local vLLM + LMCache target).

Legend: **S** = supports, **T** = threatens, **N** = novelty threat (does the same thing already).

---

## 1. State of the art by track

### 1.1 Prefix-caching internals (vLLM PagedAttention, SGLang RadixAttention)

| Source | Key claim | Verdict |
|---|---|---|
| vLLM Automatic Prefix Caching — [docs](https://docs.vllm.ai/en/stable/design/prefix_caching/) | KV blocks hashed by `hash(prefix_tokens + block_tokens)`; **SHA256 default since v0.11**; `--prefix-caching-hash-algo`; per-request `cache_salt` injected into first-block hash for multi-tenant isolation | **S** — validates content-addressed packets + salting. Exposes that our "cryptographic audit" should bind to the *block hash*, not the token text. |
| SGLang RadixAttention — [2312.07104](https://arxiv.org/abs/2312.07104); Unified Radix Cache / HiCache — [LMSYS blog](https://www.sglang.io/blog/unified-radix-cache) | Radix tree of LRU KV; 2026 Unified Radix Cache uses **copy-on-write for MAMBA checkpoints** and a shared prefix identity across **GPU/host/external tiers (L1/L2/L3)**; multi-turn hit ~98% with Mooncake L3 | **S** — the L1/L2/L3 tier identity is exactly our Tier model. **T** — CoW-for-fork is already a first-class primitive here. |
| vLLM session-aware KV hints RFC — [issue #52113](https://github.com/vllm-project/vllm/issues/52113) | Applications know future reuse; proposal for `agent_hint` metadata: session ID, retention TTL, explicit evict/prefetch ops | **S** — the ecosystem is asking for exactly the app→engine hint channel CPP would provide. |

### 1.2 Cross-agent KV reuse (LMCache, CacheBlend, CacheScout, "Request Order Matters")

| Source | Key claim | Verdict |
|---|---|---|
| LMCache — [2510.09665](https://arxiv.org/abs/2510.09665) | First/most efficient OSS KV layer: offloads+shares KV across engines/queries; control API to **pin, move, compress**; supports **non-prefix reuse**; reports **context truncation halves prefix hit ratio** | **S** — the intended substrate; audit/observe metrics align. **T** — "KV as storage/communication medium" is already claimed, so CPP cannot claim the medium as novel. |
| CacheBlend — [2405.16444](https://arxiv.org/abs/2405.16444) (EuroSys'25 Best Paper) | Reuse precomputed KV at **any position**; selectively recompute a few HKVD tokens to recover cross-attention; TTFT −2.2–3.3×, throughput +2.8–5× | **T** — undercuts "packets must be exact prefixes." **S** — proves non-prefix reuse has a fidelity cost we must price. |
| CacheScout — [2608.14624](https://arxiv.org/abs/2608.14624) | Agent-aware runtime learns agent execution transitions online → predictive eviction + prefetch; +10–18pp hit rate, TTFT −18–45%; working set = recurring fixed context = 53–62% of tokens | **N/T** — "agent anchor + agent-aware cache" is our CoW-fork story, minus the audit layer. |
| "Request Order Matters" — [2610.05833](https://arxiv.org/abs/2610.05833) | With selective recompute, **the same prompt yields different answers depending on prior requests**; answer variation 69% (CacheBlend top-*k*) → 26.1% (document-aligned recompute) at 5% budget | **T — critical.** Directly contradicts a naive "deterministic core" under non-prefix reuse. |
| Continuum — [2511.02230](https://arxiv.org/abs/2511.02230) | Tool-call-aware **KV TTL pinning** across pauses; delay −1.12–3.66× | **S** — micro-epoch/pin policy precedent; our k-step epochs generalize TTL. |
| ForkKV — [2604.06370](https://arxiv.org/abs/2604.06370) | **fork + copy-on-write disaggregated KV**: `DualRadixTree` splits shareable base cache from agent-specific residual; `ResidualAttention`; +3.0× throughput | **N — priority.** "Copy-on-Write sub-agent forking sharing an immutable base" is already published for serving. |
| AAFLOW+ — [2607.10987](https://arxiv.org/abs/2607.10987) | KV as **first-class distributed object**; operators for materialize/transfer/**fork**/compose/evict; TTFT −50.2× | **N/T** — distributed KV fork/compose operators overlap our packet algebra. |
| KVCOMM — [2510.12872](https://arxiv.org/abs/2510.12872) | Anchor KV states enable **cross-agent KV sharing** with alignment for prefix deltas; 7.8× TTFT, >70% reuse | **T** — cross-agent anchor reuse is solved at the KV layer. |

### 1.3 Context folding / compression (ReFold, AgentFold, Complexity Trap, ACON, TokenPilot, CliffCompaction, KV-Fold)

| Source | Key claim | Verdict |
|---|---|---|
| **ReFold — [2610.07863](https://arxiv.org/abs/2610.07863)** | Training-free **rendering layer** keeps append-only history, compresses only rendered context; **chunked rendering to preserve prefix-cache locality**; every removal **reversible via restore**; −2.5× tokens, −50% KV memory | **N — priority.** This is CPP's Tier 1 (append-only) + rendering + prefix alignment + reversibility, already stated as an architecture. |
| AgentFold — [2510.24699](https://arxiv.org/abs/2510.24699) (ICLR'26) | Proactive multi-scale folding; **7k tokens after 100 turns**, scales to 500 turns | **T** — proactive context budgeting, but trained (SFT) not deterministic. |
| Context-Folding — [2510.11967](https://arxiv.org/abs/2510.11967) (ICML'26) | Branch-and-return with FoldGRPO; agents branch into sub-contexts and fold results back | **S/T** — validates sub-context isolation; lossy and RL-trained. |
| The Complexity Trap — [2508.21433](https://arxiv.org/abs/2508.21433) | **Observation masking matches LLM summarization at ~half cost**; simple beats complex; observations ≈84% of agent turns | **T** — attacks the value of a heavy "compiler"; **S** — CPP must be the *principled* simple version. |
| ACON — [2510.00615](https://arxiv.org/abs/2510.00615) (ICML'26) | Failure-driven compression-guideline optimization (no weight updates); peak tokens −26–54%, +30% acc | **S/T** — deterministic-ish, but is a *predictor* pipeline (extra call, latency). |
| TokenPilot — [2606.17016](https://arxiv.org/abs/2606.17016) | Dual-granularity: **ingestion-aware compaction** stabilizes byte-identical prefix; lifecycle eviction batches updates; cost −56–87%, cache misses 1.549M | **N — priority.** "compress + never break the prompt cache" is our core pitch. |
| CliffCompaction — [2609.26779](https://arxiv.org/abs/2609.26779) | Compaction by **truncate/drop only, never rephrase**; **never compact a compaction** (drop prior output); cache-read cost −80% | **S** — validates lossless-by-deletion + no-drift. **T** — costs our "recompute summaries" differentiator. |
| KV-Fold — [2605.12471](https://arxiv.org/abs/2605.12471) | KV cache as a **left-fold accumulator** over chunks; stable drift plateau; 100% needle retrieval at 16K→128K | **S/T** — recurrence stable ⇒ k-step micro-epochs plausible; also preempts "fold" framing. |
| Compaction Cliff — [2608.22752](https://arxiv.org/abs/2608.22752) (CIKM'26) | Type-blind compaction preserves 53% of safety rules after 1 round, **10% after 5**; proposes **Knowledge Triage** by type | **S** — strong evidence for **typed** knowledge (negative/constraints as first-class), our `negative_knowledge`. |
| Online KV Compaction — [2608.00902](https://arxiv.org/abs/2608.00902) | Immediate compaction hurts; **delaying to use future queries** recovers accuracy; token-eviction more robust than attention-matching | **S** — supports deferred/batched (k-step) compaction. |

### 1.4 Agent memory / bi-temporal (Mem0, Zep/Graphiti, Letta)

| Source | Key claim | Verdict |
|---|---|---|
| Mem0 — [2504.19413](https://arxiv.org/abs/2504.19413) | Extract→consolidate→retrieve; graph variant; **91% lower p95 latency, >90% token savings**; memory as a layer, not a framework | **S** — market proof of a shared state layer. **T** — memory-as-service, not prefix-aligned packets. |
| Zep / Graphiti — [2501.13956](https://arxiv.org/abs/2501.13956) | **Bi-temporal** knowledge graph: tracks provenance *and* validity; auto-invalidates stale facts; audit + ABAC | **N — priority.** Our bi-temporal + audit claim is already a shipped industry architecture (Graphiti is OSS). |
| Letta / MemGPT — [2310.08560](https://arxiv.org/abs/2310.08560) | **Virtual context management**: OS-style memory hierarchy, agent self-edits core-memory blocks | **S** — the OS/MMU analogy is validated. **T** — the analogy is not novel; differentiator must be determinism + audit + prefix alignment. |
| Memory Trust Gap — [2609.01852](https://arxiv.org/abs/2609.01852) | Stale stored facts override authoritative current evidence; harm is capability-dependent | **S** — motivates validity/audit and negative knowledge. |

### 1.5 Prompt-cache measurement & reproducibility

| Source | Key claim | Verdict |
|---|---|---|
| Don't Break the Cache — [2601.06007](https://arxiv.org/abs/2601.06007) | Prompt caching cuts API cost **41–80%**, TTFT **13–31%**; boundary control (stable prefix, dynamic content last, exclude tool results) beats naive full-context caching | **S — strongest.** Quantifies CPP's whole value proposition on agentic workloads. |
| Same Request, Different Answer — [2609.04748](https://arxiv.org/abs/2609.04748) | Prefix caching changes agent trajectory on **37.4% (16-bit)** and **82.5% (4-bit)** of episodes; cached serving is *deterministic given cache state* but **irreproducible in practice because cache state is absent from the request** | **T — critical.** A "deterministic core" must serialize/replay cache state, or it is not reproducible. |
| ACE — [2510.04618](https://arxiv.org/abs/2510.04618) (ICLR'26) | Evolving "playbooks" with structured incremental updates avoid **context collapse**; +10.6% agents | **S/T** — validates append-only increments; "playbook" overlaps shared project state. |
| Negative Knowledge as Failure-aware Shared Memory — [2606.21024](https://arxiv.org/abs/2606.21024) | **Curator agent writes typed negative-knowledge records** to a shared bank; agents adopt/reject before next experiment; fewer tokens, solves tasks baselines fail | **N — priority.** First-class `negative_knowledge` is already published. |

---

## 2. Top 5 risks to our current claims

1. **"Deterministic core" is contradicted by cache-history sensitivity.** [2610.05833] shows the *same prompt → different answer* under persistent selective-reuse; [2609.04748] shows 37–83% trajectory divergence from prefix caching alone. **Claim at risk: deterministic/auditable core.** Mitigation: define determinism as *given a serialized cache-state epoch*, and make the epoch (not the prompt) the auditable unit. Anything else is false advertising.

2. **CoW sub-agent forking is not novel — it is shipping.** [2604.06370] ForkKV already does `fork + copy-on-write disaggregated KV` with a shared base + per-agent residual; [2607.10987] AAFLOW+ exposes `fork`/`compose` KV operators; SGLang's Unified Radix Cache uses CoW for MAMBA checkpoints. **Claim at risk: "Copy-on-Write sub-agent forking that shares immutable Tier 0."** Differentiator must be *auditable* fork lineage, not the fork mechanism.

3. **"Compile into prefix-aligned, cache-safe packets" is already an explicit design goal.** TokenPilot [2606.17016] (ingestion-aware compaction for byte-identical prefixes), ReFold [2610.07863] (chunked rendering to preserve prefix-cache locality), and CliffCompaction [2609.26779] (deletion-only, no re-compaction, cache-read −80%) collectively cover the pitch. **Claim at risk: the compiler's core loop.**

4. **First-class `negative_knowledge` is pre-empted.** [2606.21024] is literally "typed negative-knowledge shared memory" with a curator agent; [2608.22752] independently argues type-aware retention (Knowledge Triage) because type-blind compaction destroys constraints 5× faster. **Claim at risk: novelty of `negative_knowledge` and typed tiers.**

5. **The "tiny L1 ≈ 3k dynamic tokens" budget may be economically wrong.** [2508.21433] finds masking beats summarization at half cost; LMCache [2510.09665] and KCliffCompaction find **cache reads dominate** (78% of uncompacted bill) and that truncation halves hit ratio — i.e., over-aggressive minimization can *increase* total cost. **Claim at risk: ~3k token target as universal optimum.** It should be cache-cost-optimal, not token-minimal, and workload-dependent.

---

## 3. Top 5 opportunities to exploit

1. **Audit/replay is unclaimed even though the problem is proven.** [2609.04748] and [2610.05833] identify cache-induced irreproducibility but only *diagnose* it; nobody ships a serializable cache-state epoch + provenance. CPP's cryptographic audit is the natural home — make the epoch the signed artifact and expose a replay API. This is the sharpest remaining moat.

2. **Ride the app→engine hint channel.** vLLM's session-aware KV hints RFC [#52113], CacheScout's agent transitions [2608.14624], and Continuum's TTL [2511.02230] all prove apps know future reuse better than the engine. CPP can be the *compiler* that emits standardized `agent_hint`s (session identity, TTL, evict/prefetch) — a compatibility layer, not a competitor.

3. **Deferred, batched micro-epochs are evidence-backed.** [2608.00902] (delayed compaction recovers accuracy), ReFold's chunked rendering [2610.07863], and TokenPilot's batch-turn eviction [2606.17016] all independently show *not* rewriting every step wins. Our k-step micro-epoch is the principled generalization — market it as "the epoch boundary is a cache-cost knob."

4. **Typed tiers (negative/constraints/metadata) are validated but unsolved for deterministic compilers.** [2608.22752] quantifies the catastrophe of type-blind compaction; [2606.21024] shows typed negative knowledge transfers across tasks. A deterministic, prefix-aligned typed-tier compiler is still open.

5. **LMCache + vLLM is the right, already-adopted substrate.** [2510.09665] gives pin/move/compress/lookup APIs plus observability; vLLM block hashing gives the content-addressed identity. Build CPP as an LMCache connector + vLLM `agent_hint` producer rather than a new serving stack — lowest adoption friction.

---

## 4. Papers that already do what we claim (priority novelty threats)

| # | Paper | CPP claim pre-empted |
|---|---|---|
| 1 | **ReFold** [2610.07863](https://arxiv.org/abs/2610.07863) | Append-only history + compressed *rendering* + **prefix-cache locality via chunked rendering** + reversibility. Nearest single threat to the whole Tier-0/1 + rendering model. |
| 2 | **ForkKV** [2604.06370](https://arxiv.org/abs/2604.06370) | **CoW sub-agent fork** sharing an immutable base cache with per-agent residual. |
| 3 | **TokenPilot** [2606.17016](https://arxiv.org/abs/2606.17016) | Compile context **without breaking the prompt cache** (ingestion-aware, canonicalized prefix). |
| 4 | **Zep/Graphiti** [2501.13956](https://arxiv.org/abs/2501.13956) | **Bi-temporal** memory with provenance + validity + auto-invalidation + audit. |
| 5 | **Negative Knowledge Memory** [2606.21024](https://arxiv.org/abs/2606.21024) | **First-class typed negative knowledge** in shared multi-agent memory. |
| 6 | **CacheBlend** [2405.16444](https://arxiv.org/abs/2405.16444) | Non-prefix KV packet reuse with selective recompute. |
| 7 | **KV-Fold** [2605.12471](https://arxiv.org/abs/2605.12471) | The "fold" recurrence framing / k-step accumulation. |

**Net:** the *components* are increasingly published. The unoccupied ground is the **union**: a **deterministic, replayable, cryptographically auditable epoch** that sits *above* LMCache/vLLM and emits standard cache hints — plus the claim of being the first to make **cache-state part of the signed provenance**.

---

## 5. Recommended experiments to stay ahead

1. **Reproduction harness for cache-induced nondeterminism.** Replicate [2609.04748]/[2610.05833] on our vLLM+LMCache target: measure trajectory divergence with cache on/off and across request orders. Then demonstrate CPP's **serialized epoch makes the run bit-reproducible on replay**. Success: 0 divergence across N reorders with epoch pinning vs. baseline divergence. *This is the single most defensible result we can own.*

2. **Audit-cost benchmark.** Quantify overhead of signing/binding the epoch (SHA256 block hash + provenance) vs. [2601.06007]'s 41–80% cost savings and [2609.26779]'s cache-read accounting. Success: audit overhead <5% while preserving ≥90% of cache savings.

3. **Head-to-head vs. ReFold / TokenPilot / CliffCompaction** on Terminal-Bench / SWE-bench Verified under a bounded window: compare *total provider cost* (cache-read included), task success, and **prefix-hit ratio**. Success: CPP ≥ parity on success with lower cost **and** a strictly higher, stable hit ratio (i.e., we never break the cache).

4. **Typed-tier ablations for `negative_knowledge`.** Reproduce the Compaction Cliff [2608.22752] safety-rule retention curves; show typed negative/constraint tiers preserve ≥95% of rules after 5 compaction rounds vs. 10% type-blind baseline. Also test transfer across tasks ([2606.21024]).

5. **Fork-lineage provenance for CoW sub-agents.** Extend ForkKV-style CoW with a signed fork DAG (parent epoch → child residual) and verify auditability/replay; measure hit-rate and TTFT parity with CacheScout [2608.14624] / ForkKV. Success: parity perf + full fork lineage reconstruction.

6. **Budget–cost frontier study.** Sweep the "~3k token" target against cache-read cost to find the cache-cost-optimal budget per workload (not the token-minimal one), answering risk #5 with data.

7. **Standardize `agent_hint` emission.** Implement a vLLM `agent_hint` producer (per RFC #52113) from CPP epochs and measure hit-rate/TTFT lift vs. CacheScout and Continuum baselines.

---

## 6. Source index

- vLLM APC docs — https://docs.vllm.ai/en/stable/design/prefix_caching/
- vLLM session-aware KV hints RFC — https://github.com/vllm-project/vllm/issues/52113
- SGLang RadixAttention — arXiv:2312.07104 · Unified Radix Cache — https://www.sglang.io/blog/unified-radix-cache
- LMCache — arXiv:2510.09665 · https://docs.lmcache.ai/
- CacheBlend — arXiv:2405.16444
- CacheScout — arXiv:2608.14624
- Request Order Matters — arXiv:2610.05833
- Continuum — arXiv:2511.02230
- ForkKV — arXiv:2604.06370
- AAFLOW+ — arXiv:2607.10987
- KVCOMM — arXiv:2510.12872
- ReFold — arXiv:2610.07863
- AgentFold — arXiv:2510.24699
- Context-Folding — arXiv:2510.11967
- The Complexity Trap — arXiv:2508.21433
- ACON — arXiv:2510.00615
- TokenPilot — arXiv:2606.17016
- CliffCompaction — arXiv:2609.26779
- Online KV Compaction — arXiv:2608.00902
- Compaction Cliff — arXiv:2608.22752
- KV-Fold — arXiv:2605.12471
- Mem0 — arXiv:2504.19413
- Zep / Graphiti — arXiv:2501.13956
- Letta / MemGPT — arXiv:2310.08560
- Memory Trust Gap — arXiv:2609.01852
- Negative Knowledge Memory — arXiv:2606.21024
- ACE — arXiv:2510.04618
- Don't Break the Cache — arXiv:2601.06007
- Same Request, Different Answer — arXiv:2609.04748
