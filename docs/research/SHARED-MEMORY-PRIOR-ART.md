# Shared Memory Prior Art — What Prior Art Says About "the Shared Copy"

- **Date:** 2026-10-17. **Scope:** cross-agent memory/KV sharing, copy-on-write forking, shared-state consistency, and shipping memory products, 2024–Oct 2026.
- **Target concept:** cross-agent shared memory built on `EPOCH_FOLD` (k-step append-only micro-epochs — `docs/design/Arch-Spec.md` §1.5) and `COW_FORK` (`parent_packet_id` children — `docs/decisions/ADR-0005-packet-schema-v1.md` line 59), where children share immutable Tier 0/1 and discard their Tier 3 (`docs/design/Arch-Spec.md` §1.7).
- **Method:** web/arXiv research only, primary sources preferred (papers, engine docs, product docs, GitHub). No code changed; this is one research file. Every claim carries a URL. Vendor benchmarks are self-published unless noted. Items that could not be verified directly are marked **[UNVERIFIED]**. Companion: `docs/research/SOTA-AND-THREATS.md` (2026-10-16) covers prefix-caching internals; this file does not re-derive them.

### Questions this document answers

- **Who already shares memory/KV across agents — production vs research?** → §1.
- **What fork/copy-on-write semantics exist, and what do they cost?** → §2.
- **Which agent-memory products support multi-agent sharing, and what do users complain about?** → §3.
- **What breaks when memory is shared (consistency, staleness, leakage, poisoning)?** → §4.
- **What should Context Fabric's "shared copy" actually be, and what must we not claim?** → §5–§6.
- Plus: **what we could not verify** → §7.

This is decision input for the `COW_FORK`/`EPOCH_FOLD` design; it is not a request to change code.

## Blunt read (before the evidence)

1. **Sharing KV across requests and agents is default production behavior, today.** vLLM APC is on by default in v1 ([docs](https://docs.vllm.ai/en/stable/design/prefix_caching/)); LMCache ships explicit cross-engine P2P sharing ([docs](https://docs.lmcache.ai/getting_started/quickstart/share_kv_cache.html)); Mooncake runs a cross-instance KV pool behind Kimi ([repo](https://github.com/kvcache-ai/Mooncake)). "Agents share a cache" is not a feature — it is the substrate.
2. **CoW forking of a shared base is published and partly shipped.** ForkKV does fork + CoW disaggregated KV for multi-LoRA agent fleets ([2604.06370](https://arxiv.org/abs/2604.06370)); SGLang's Unified Radix Cache uses copy-on-write for Mamba checkpoints ([blog](https://www.sglang.io/blog/unified-radix-cache)); AAFLOW+ exposes `fork`/`compose` KV operators ([2607.10987](https://arxiv.org/abs/2607.10987)). The *mechanism* is not defensible as novel.
3. **The genuinely unoccupied ground is the contract, the lineage, and the replay.** The 2026 KV-cache survey observes shared caches "rely on reference counting, copy-on-write, and location directories, but rarely state a consistency contract," and no surveyed system defines durable KV recovery ([2607.02574](https://arxiv.org/abs/2607.02574)). Two 2026 papers prove cache-induced irreproducibility and only diagnose it ([2609.04748](https://arxiv.org/abs/2609.04748), [2610.05833](https://arxiv.org/abs/2610.05833)).
4. **Multi-agent *memory* (not KV) is objectively broken right now.** The best system on GroupMemBench averages ~46%, and BM25 matches most "memory" systems ([2605.14498](https://arxiv.org/abs/2605.14498)). Letta's own docs call concurrent writes to shared blocks an anti-pattern that loses updates ([docs](https://docs.letta.com/guides/core-concepts/memory/shared-memory)). Any correctness win here is measurable and defensible.
5. **Therefore the position is: governance + audit layer *over* shared KV — not a new sharing mechanism.** Ride vLLM/LMCache/ForkKV; own the consistency contract, the signed fork lineage, and the replay API. If the shared copy is only "agents reuse each other's KV," it should not be built — that fight is already lost to engines.

---

## 1. Who already shares — production vs research

### 1.1 Production (or merged upstream)

- **vLLM Automatic Prefix Caching (default in v1).** KV blocks hashed `hash(prefix_tokens + block_tokens)`; SHA256 default since v0.11; per-request `cache_salt` injected into the first-block hash for multi-tenant isolation; block table is append-only and blocks are ref-counted. Any two requests (or agents) sharing a prefix share GPU memory for free. Limits: engine-local, prefix-only, prefill-only, no lineage, no app-visible contract. Sources: [vLLM APC design](https://docs.vllm.ai/en/stable/design/prefix_caching/), [v1 prefix-caching design](https://docs.vllm.ai/en/v0.8.0/design/v1/prefix_caching.html), [v0.10 features](https://docs.vllm.ai/en/v0.10.0/features/automatic_prefix_caching.html).
- **SGLang RadixAttention / HiCache.** Radix tree over KV with LRU eviction; per-namespace isolation via `extra_key`/cache_salt in `radix_cache.py`; 2026 Unified Radix Cache spans GPU/host/external tiers (L1/L2/L3) and uses copy-on-write for Mamba checkpoints; multi-turn hit ~98% with Mooncake as L3. Sources: [SGLang paper](https://arxiv.org/abs/2312.07104), [Unified Radix Cache blog](https://www.sglang.io/blog/unified-radix-cache), [radix_cache.py](https://github.com/sgl-project/sglang/blob/main/python/sglang/srt/mem_cache/radix_cache.py).
- **LMCache.** OSS KV layer: offload, centralize, and share KV across engines and queries; P2P sharing over RDMA (PyTorch Foundation project); also ships a CacheBlend "blend" engine for non-prefix reuse via selective recompute. Multi-process sharing requires deterministic hashing (`PYTHONHASHSEED=0`). Sources: [share-kv-cache docs](https://docs.lmcache.ai/getting_started/quickstart/share_kv_cache.html), [docs home](https://docs.lmcache.ai/), [LMCache paper](https://arxiv.org/abs/2510.09665), [CacheBlend](https://arxiv.org/abs/2405.16444).
- **Mooncake.** KV-cache-centric disaggregated architecture behind Kimi's production serving; reports "75% more requests handled" with cache reuse (self-published); vLLM added Mooncake Store-backed connectors in 2026. Sources: [Mooncake repo](https://github.com/kvcache-ai/Mooncake), [Mooncake paper](https://arxiv.org/abs/2407.00079), [Go module listing](https://pkg.go.dev/github.com/kvcache-ai/mooncake@v0.3.13-rc1).
- **vLLM session-aware KV hints RFC** ([issue #52113](https://github.com/vllm-project/vllm/issues/52113)): proposal for app-supplied `agent_hint` metadata (session ID, TTL, evict/prefetch) — the app→engine hint channel our compiler should feed. Status: open RFC, not shipped.

### 1.2 Research (cross-agent specific)

- **KVCOMM** ([2510.12872](https://arxiv.org/abs/2510.12872), NeurIPS 2025): cross-agent KV reuse via shared anchor states with offset correction; >70% reuse, 7.8× TTFT. Training-free, approximate.
- **ForkKV** ([2604.06370](https://arxiv.org/abs/2604.06370)): fork + CoW disaggregated KV for multi-LoRA agent fleets; `DualRadixTree` (shared base + per-agent residual), +3.0× throughput. The strongest articulation of our own fork idea, already published at the kernel/service layer.
- **AAFLOW+** ([2607.10987](https://arxiv.org/abs/2607.10987)): KV as a first-class distributed object with materialize/transfer/fork/compose/evict operators; TTFT −50.2× claimed. **Caveat:** results come from an analytical cost model, not a full serving deployment.
- **CacheScout** ([2608.14624](https://arxiv.org/abs/2608.14624)): agent-aware runtime on vLLM that learns agent execution transitions online to drive eviction + prefetch; +10–18pp hit rate, TTFT −18–45%, peak throughput +57%. Policy only — no audit or consistency layer.
- **Continuum** ([2511.02230](https://arxiv.org/abs/2511.02230)): KV TTL pinning across tool-call pauses (pinning ≈ our epoch retention policy). **Tokencake** ([2510.18586](https://arxiv.org/abs/2510.18586), EuroSys'27): agent-aware KV scheduling.
- **Jenga** (SOSP 2025, [DOI](https://dl.acm.org/doi/10.1145/3731569.3764823)): heterogeneous KV-cache allocator — improves caching but is **not** cross-agent sharing. **EPIC** (ICML 2025, [PMLR](https://proceedings.mlr.press/v267/hu25j.html)): position-independent chunk reuse, up to 8× TTFT — proves non-prefix reuse, with recompute overhead and fidelity risk.

### 1.3 At which layer does sharing already happen?

| Layer | Already shared (production) | Missing there |
|---|---|---|
| GPU/engine KV | Prefix KV (vLLM APC, SGLang), cross-instance pools (Mooncake), P2P (LMCache) | Consistency contract, lineage, replay ([2607.02574](https://arxiv.org/abs/2607.02574)) |
| Serving/workflow policy | Agent-aware eviction/pinning (CacheScout, Continuum, Tokencake) | Ownership of shared state; conflict semantics |
| Agent memory (app) | Shared blocks (Letta), org scopes (Mem0), group graphs (Zep/Graphiti) | Multi-party correctness (~46% best, [2605.14498](https://arxiv.org/abs/2605.14498)); merge contracts |
| Packet/epoch layer (ours) | — | The contract + lineage + replay — still unclaimed |

## 2. Fork / copy-on-write semantics: what exists, what it costs

### 2.1 Shipping semantics

- vLLM: ref-counted block sharing + append-only block table = implicit CoW at block granularity; no user-facing "fork" object and no lineage ([design doc](https://docs.vllm.ai/en/v0.8.0/design/v1/prefix_caching.html)).
- SGLang: CoW for Mamba checkpoints inside Unified Radix Cache; prefix tree sharing for transformer KV ([blog](https://www.sglang.io/blog/unified-radix-cache)).
- LMCache/Mooncake: *copy/transfer* of KV chunks (pin, move, compress) — not CoW semantics ([docs](https://docs.lmcache.ai/)).

### 2.2 Published fork semantics

- ForkKV ([2604.06370](https://arxiv.org/abs/2604.06370)): real fork + CoW with a shared base cache and per-agent residual — the canonical citation for "don't claim the mechanism."
- AAFLOW+ ([2607.10987](https://arxiv.org/abs/2607.10987)): `fork`/`compose` operators over distributed KV objects.
- Context-Folding ([2510.11967](https://arxiv.org/abs/2510.11967)) branches and returns sub-contexts (RL-trained, lossy); KV-Fold ([2605.12471](https://arxiv.org/abs/2605.12471)) frames KV as a fold accumulator.

### 2.3 What sharing costs in answer quality (measured, not theoretical)

- Selective reuse makes answers a function of *request order*: answer variation 69.0% with CacheBlend-style token top-k vs 26.1% with document-aligned recompute at a matched 5% recompute budget, at ~5.7× median TTFT either way ([2610.05833](https://arxiv.org/abs/2610.05833)).
- Prefix caching alone changes agent trajectories on 37.4% (16-bit) / 82.5% (4-bit) of episodes; runs are deterministic *given cache state*, but cache state is absent from the request — hence irreproducible in practice ([2609.04748](https://arxiv.org/abs/2609.04748)).
- KV reuse can silently flip LLM-judge selections while end-task accuracy looks stable; reuse weakens cross-candidate attention ([2601.08343](https://arxiv.org/abs/2601.08343)).

**Conclusion:** fork+CoW is table stakes; the open ground is **fork lineage + replay + the consistency contract**, which neither ForkKV nor AAFLOW+ addresses.

## 3. Agent-memory products: multi-agent support and complaints

- **Letta (MemGPT lineage).** Real shared-memory primitive: shared memory blocks visible across agents, read-only blocks, and documented anti-patterns — concurrent `memory_rethink` on the same block causes lost updates ([shared memory docs](https://docs.letta.com/guides/core-concepts/memory/shared-memory)). Product surface has moved repeatedly (V1 server retired in favor of `letta-code`, [repo](https://github.com/letta-ai/letta)); public benchmark dispute: Letta showed a filesystem+search baseline outperforming vendor-claimed memory scores ([benchmarking blog](https://www.letta.com/blog/benchmarking-ai-agent-memory/)).
- **Mem0.** Extract→consolidate→retrieve memory layer ([2504.19413](https://arxiv.org/abs/2504.19413)); scopes include agent/org, but multi-agent merge semantics are not a first-class, documented contract. Benchmark trust is contested: Mem0's own comparison claims 94.4 LongMemEval vs Zep 71.2 ([self-published](https://mem0.ai/library/agent-memory/mem0-vs-zep)); Zep publicly disputed the methodology ([rebuttal](https://www.getzep.com/blog/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/)).
- **Zep / Graphiti.** Bi-temporal knowledge graph with provenance and validity windows ([2501.13956](https://arxiv.org/abs/2501.13956)); strongest prior art for validity semantics. Zep Community Edition was deprecated with minimal notice ([PR #390](https://github.com/getzep/zep/pull/390), [features retired May 2025](https://blog.getzep.com/zep-feature-retirements-may-2025)) — a self-host-withdrawal data point; open-source effort collapsed to Graphiti.
- **cognee.** Graph+vector memory claiming multi-agent shared memory via MCP/ACL ([self-published comparison](https://www.cognee.ai/open-source-memory-frameworks-llm-agents)). Treat claims as vendor-authored.
- **LangMem (LangChain).** Namespaced store for LangGraph; single-vendor coupling; **low activity** — last PyPI release 0.0.30 (27 Oct 2025) ([PyPI](https://pypi.org/project/langmem/)).
- **2026 newcomers:** MemOS — "MemCube" composable/migratable memory units ([2507.03724](https://arxiv.org/abs/2507.03724)); Collaborative Memory — explicit private/shared tiers with access graphs and immutable provenance ([2505.18279](https://arxiv.org/abs/2505.18279)); Hindsight — best-known multi-party results but collapses in groups ([2512.12818](https://arxiv.org/abs/2512.12818)).
- **Benchmark distrust is itself a finding.** Vendor benchmarks (Mem0 vs Zep; LoCoMo methodology disputes) are not usable as evidence; treat any vendor-published comparison as a claim to reproduce, not a fact ([Mem0 comparison](https://mem0.ai/library/agent-memory/mem0-vs-zep), [Zep rebuttal](https://www.getzep.com/blog/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/)).
- **The demand-side verdict:** GroupMemBench ([2605.14498](https://arxiv.org/abs/2605.14498)) finds multi-party knowledge collapse; strongest system averages ~46%, and BM25 matches most "memory" systems — multi-party shared memory is **unsolved**. Belief-updating memory with provenance caps outperforms last-write-wins under conflicting writes ([2606.22030](https://arxiv.org/abs/2606.22030)); a lineage-enforcement defense was cited there ([2605.14421](https://arxiv.org/abs/2605.14421)). Letta's lost-update anti-pattern (§3 above) corroborates the failure directly.

## 4. What breaks when memory is shared

| Failure mode | Evidence | Who addresses it | Gap for us |
|---|---|---|---|
| Lost updates / concurrent writes | Letta anti-pattern ([docs](https://docs.letta.com/guides/core-concepts/memory/shared-memory)) | Nobody ships ownership semantics for agents | Single-writer/lease + version vectors at `EPOCH_FOLD` |
| Stale facts override current evidence | Memory Trust Gap ([2609.01852](https://arxiv.org/abs/2609.01852)); STALE best model 55.2% ([2605.06527](https://arxiv.org/abs/2605.06527)) | Bi-temporal validity (Graphiti); belief update + provenance caps ([2606.22030](https://arxiv.org/abs/2606.22030)) | Deterministic freshness gates already in our policy engine |
| Cross-tenant leakage via shared cache | vLLM `cache_salt` ([docs](https://docs.vllm.ai/en/stable/design/prefix_caching/)); SGLang `extra_key`/salt ([radix_cache.py](https://github.com/sgl-project/sglang/blob/main/python/sglang/srt/mem_cache/radix_cache.py)) | Engine-level salting | Per-trust-group salt at packet layer (ADR-0005 has `trust_domain`) |
| Poisoning via shared stores | MINJA 98.2% injection / 76.8% ASR, query-only ([2503.03704](https://arxiv.org/abs/2503.03704)); eTAMP ([2604.02623](https://arxiv.org/abs/2604.02623)); MemPoison ([2607.14651](https://arxiv.org/abs/2607.14651)); MemGhost single-email ([CSA note](https://labs.cloudsecurityalliance.org/wp-content/uploads/2026/07/CSA_research_note_memghost_agent_memory_poisoning_20260723-csa-styled.pdf)); survey ([2604.16548](https://arxiv.org/abs/2604.16548)) | OWASP ASI06 names it ([OWASP](https://genai.owasp.org/2026/05/13/memory-is-a-feature-it-is-also-an-attack-surface/)); MAPLE-Guard lifecycle guard ([2608.00426](https://arxiv.org/abs/2608.00426)) | Write-time quarantine + provenance tags (nobody ships this for agents) |
| Selective sharing / access scope | Collaborative Memory private/shared tiers + access graphs ([2505.18279](https://arxiv.org/abs/2505.18279)); ShareMem ([2609.32511](https://arxiv.org/abs/2609.32511)) | Research-grade only | Our RBAC/trust-domain gates are the productized version |
| Behavioral nondeterminism from cache history | Order-dependence ([2610.05833](https://arxiv.org/abs/2610.05833)); trajectory divergence ([2609.04748](https://arxiv.org/abs/2609.04748)); judge flips ([2601.08343](https://arxiv.org/abs/2601.08343)) | Diagnosed only — nobody ships the fix | Signed epoch manifest + replay API (the moat) |

Additional context: the 2026 survey ([2607.02574](https://arxiv.org/abs/2607.02574)) states consistency contracts for shared caches are rarely stated and durable KV has no explicit recovery model; OWASP added memory/context poisoning as ASI06 ([link](https://genai.owasp.org/2026/05/13/memory-is-a-feature-it-is-also-an-attack-surface/)).

## 5. Comparison table

| System | What it shares | Mechanism / contract | Limits | Status |
|---|---|---|---|---|
| vLLM APC | Prefix KV across requests/agents | Hash-matched blocks, ref counts, `cache_salt`; no consistency contract, no lineage | Engine-local, prefix-only, invisible to app | **Production default** |
| SGLang Radix/HiCache | Prefix KV, L1/L2/L3 tiers; CoW for Mamba checkpoints | Radix tree + `extra_key` namespaces; no cross-agent contract | Engine-local, prefix-only semantics | **Production** |
| LMCache | KV across engines/queries | Central + P2P (RDMA), pin/move/compress; blend engine non-prefix | Trusts app for correctness; no contracts | **OSS production** |
| Mooncake | KV across instances | Disaggregated pool, RDMA, Store | Self-published perf; no cross-agent semantics | **Production @ Kimi** |
| CacheBlend / EPIC | Non-prefix KV chunks | Selective recompute; position-independent | Fidelity cost; CacheBlend order-sensitivity proven | Research → OSS |
| Jenga | Heterogeneous cache pools | Allocator only | Not agent-aware, not cross-agent | SOSP'25 |
| KVCOMM | Cross-agent anchor KV | Offset correction, training-free | Approximate; quality risk in judges | NeurIPS'25 |
| ForkKV | Fork+CoW KV base for agent fleets | `DualRadixTree` base+residual | Research system; no lineage/audit | Published 2026 |
| AAFLOW+ | Distributed KV objects | fork/compose operators | Analytical cost model, not deployed | Published 2026 |
| CacheScout / Continuum / Tokencake | Agent-aware eviction/pinning | Learned or TTL policy on vLLM/LMCache | Policy only; no shared-state governance | 2026 research |
| Letta | Shared memory blocks across agents | Read-only option; **lost updates on concurrent writes** | Coarse; anti-pattern documented | OSS product |
| Mem0 | User/agent/org-scoped memories | Extract-consolidate-retrieve; no merge contract | Benchmark disputes; single-writer assumed | VC-backed OSS/svc |
| Zep / Graphiti | Bi-temporal facts, group graphs | Validity intervals + provenance | CE deprecated; self-host = Graphiti only | OSS + cloud |
| cognee | Graph memory via MCP | Vendor-claimed ACL sharing | Evidence vendor-authored | OSS startup |
| LangMem | Namespaced store | LangGraph store namespaces | Single-agent; stagnation (no release since 10/2025) | OSS, low activity |
| MemOS | MemCube units (compose/migrate) | Design goal: memory as OS resource | Early; claims unverified | Research + OSS |
| GroupMemBench | (benchmark) | Measures multi-party collapse | Best system ~46% | 2026 benchmark |

## 6. So what for Context Fabric

Ranked; "build" items are the only defensible moat given §1–§4.

1. **BUILD — the consistency contract + ownership semantics for shared copies** (leases or single-writer per Tier 1 block; version vectors; explicit conflict policies resolved deterministically at `EPOCH_FOLD`). Directly addresses the survey's "rarely state a consistency contract" gap ([2607.02574](https://arxiv.org/abs/2607.02574)) and the lost-update failure Letta documents ([docs](https://docs.letta.com/guides/core-concepts/memory/shared-memory)). Nothing surveyed ships this for agents.
2. **BUILD — signed fork/epoch lineage + replay.** `COW_FORK` children carry `parent_packet_id` (ADR-0005); extend to signed epoch manifests that embed cache-state identity (model + quantization + cache_salt/extra_key + block hashes) and expose a replay API that pins that state. The problem is documented ([2609.04748](https://arxiv.org/abs/2609.04748), [2610.05833](https://arxiv.org/abs/2610.05833)); nobody ships the artifact. Claim scope: "reproducible *given a serialized epoch*", never "deterministic model output".
3. **BUILD — write-time governance for shared memory:** provenance-tagged writes, poison quarantine at fold boundaries, per-trust-group salt (vLLM `cache_salt` semantics, [docs](https://docs.vllm.ai/en/stable/design/prefix_caching/)). Align with OWASP ASI06 ([link](https://genai.owasp.org/2026/05/13/memory-is-a-feature-it-is-also-an-attack-surface/)); test against MINJA-style query-only injections ([2503.03704](https://arxiv.org/abs/2503.03704)). Multi-party memory is at ~46% ([2605.14498](https://arxiv.org/abs/2605.14498)) — any correctness win here is publishable.
4. **INTEGRATE — do not build a KV engine.** Target vLLM APC + LMCache as substrate; emit `agent_hint`s per RFC #52113 ([issue](https://github.com/vllm-project/vllm/issues/52113)); if LMCache P2P is present, the shared copy is the packet-level abstraction *over* it. This mirrors the "ride the substrate" conclusion in `SOTA-AND-THREATS.md` §3.5.
5. **DO NOT BUILD:** a new radix tree/allocator (Jenga, SGLang own it), a KV transfer layer (LMCache/Mooncake own it), a CoW-fork primitive (ForkKV owns it), or another general memory graph (Graphiti/Mem0 own it). Each has a shipping incumbent and an adoption moat.
6. **DO NOT CLAIM:** CoW-fork novelty; cross-agent sharing novelty; non-prefix reuse novelty; deterministic outputs under shared caches. **DO CLAIM:** the shared copy as *auditable governance over* shared KV — consistency contract, signed lineage, deterministic packet content, and end-to-end measured role-divergent reuse (README's 9.5× (N2 caveat: no quality gate) / 8.23× throughput / 20.2× KV figures — the KV range is vs unshared raw; ~2× vs shared — remain subject to `docs/engineering/VERIFICATION-REPORT.md`).
7. **MEASURE honestly:** head-to-head against a vLLM+LMCache baseline (not naive broadcast); report total cost including cache reads; report answer-variation across request orders ([2610.05833](https://arxiv.org/abs/2610.05833) methodology) as a first-class metric; publish negative results; treat AAFLOW+'s analytical numbers ([2607.10987](https://arxiv.org/abs/2607.10987)) as an upper bound, not a target.
8. **SEQUENCE:** (1)→(2)→(3) is the moat; (4) is the substrate; (6) is the messaging discipline. If reviewers ask "what's new vs ForkKV + LMCache?", the answer must be: *the contract, the lineage, and the replay — not the sharing*.

### Explicitly do NOT, in v1

- Do not build or fork a KV cache engine, radix tree, or transfer layer — vLLM/SGLang/LMCache/Mooncake/ForkKV own every layer of that stack.
- Do not claim reproducibility or determinism of model *outputs*; only of the compiled packet and the epoch state.
- Do not adopt vendor benchmark numbers (Mem0/Zep/Letta/Cognee) as our baseline — reproduce on our stack or omit.
- Do not treat "sharing" as the pitch; every competitor already shares. The pitch is governance, lineage, and replay.

### Bottom line

The shared copy's *mechanism* is commodity (vLLM APC today, ForkKV tomorrow). The shared copy's *value* is the part nobody ships: an explicit consistency contract for shared state, a signed fork/epoch lineage that makes shared runs replayable, and write-time poison governance. Build those three; integrate everything else; refuse to claim the mechanism as novel.

## 7. What we could not verify

- vLLM RFC #52113 and LMCache P2P GA date as cited in our own `SOTA-AND-THREATS.md` — carried forward from that companion doc; engine docs cited here cover the shipped mechanisms.
- Any specific production number from Mooncake (75% more requests) and AAFLOW+ (−50.2×) is self-published/analytical and should be treated as claims.

## 8. Source index

- vLLM APC — https://docs.vllm.ai/en/stable/design/prefix_caching/ · v1 design — https://docs.vllm.ai/en/v0.8.0/design/v1/prefix_caching.html
- vLLM session hints RFC — https://github.com/vllm-project/vllm/issues/52113
- SGLang — https://arxiv.org/abs/2312.07104 · https://www.sglang.io/blog/unified-radix-cache · https://github.com/sgl-project/sglang/blob/main/python/sglang/srt/mem_cache/radix_cache.py
- LMCache — https://docs.lmcache.ai/getting_started/quickstart/share_kv_cache.html · https://arxiv.org/abs/2510.09665
- Mooncake — https://github.com/kvcache-ai/Mooncake · https://arxiv.org/abs/2407.00079
- CacheBlend — https://arxiv.org/abs/2405.16444 · Jenga — https://dl.acm.org/doi/10.1145/3731569.3764823 · EPIC — https://proceedings.mlr.press/v267/hu25j.html
- KVCOMM — https://arxiv.org/abs/2510.12872 · ForkKV — https://arxiv.org/abs/2604.06370 · AAFLOW+ — https://arxiv.org/abs/2607.10987
- CacheScout — https://arxiv.org/abs/2608.14624 · Continuum — https://arxiv.org/abs/2511.02230 · Tokencake — https://arxiv.org/abs/2510.18586
- Cache-history papers — https://arxiv.org/abs/2610.05833 · https://arxiv.org/abs/2609.04748 · https://arxiv.org/abs/2601.08343
- KV survey — https://arxiv.org/abs/2607.02574
- Letta — https://docs.letta.com/guides/core-concepts/memory/shared-memory · https://www.letta.com/blog/benchmarking-ai-agent-memory/ · https://github.com/letta-ai/letta
- Mem0 — https://arxiv.org/abs/2504.19413 · https://mem0.ai/library/agent-memory/mem0-vs-zep
- Zep/Graphiti — https://arxiv.org/abs/2501.13956 · https://github.com/getzep/zep/pull/390 · https://blog.getzep.com/zep-feature-retirements-may-2025 · https://www.getzep.com/blog/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/
- cognee — https://www.cognee.ai/open-source-memory-frameworks-llm-agents · LangMem — https://pypi.org/project/langmem/
- MemOS — https://arxiv.org/abs/2507.03724 · Collaborative Memory — https://arxiv.org/abs/2505.18279 · GroupMemBench — https://arxiv.org/abs/2605.14498
- Belief memory — https://arxiv.org/abs/2606.22030 · STALE — https://arxiv.org/abs/2605.06527 · Trust Gap — https://arxiv.org/abs/2609.01852 · ShareMem — https://arxiv.org/abs/2609.32511
- Poisoning — https://arxiv.org/abs/2604.16548 · https://arxiv.org/abs/2503.03704 · https://arxiv.org/abs/2604.02623 · https://arxiv.org/abs/2607.14651 · https://arxiv.org/abs/2608.00426
- OWASP — https://genai.owasp.org/2026/05/13/memory-is-a-feature-it-is-also-an-attack-surface/ · CSA notes — https://labs.cloudsecurityalliance.org/wp-content/uploads/2026/07/CSA_research_note_memghost_agent_memory_poisoning_20260723-csa-styled.pdf · https://labs.cloudsecurityalliance.org/wp-content/uploads/2026/04/CSA_research_note_ai_infrastructure_vulnerabilities_sglang_mcp_20260424-csa-styled.pdf
