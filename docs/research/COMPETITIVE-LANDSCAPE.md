# Context Fabric — Competitive Landscape (October 2026)

Scope: who else is solving "what the agent sees," and where CPP v1.0 wins, loses, or should integrate.
Basis: public docs/papers/pricing as of 2026-10-16, plus our own Stage-B audit (`docs/engineering/AUDIT.md`).

## Executive read

- **The category is real and crowded.** "Context engineering" is now a Gartner/Thoughtworks-adopted discipline; Google ADK literally ships "context as a compiled view," and LangChain ships compaction + subagents in the default harness. We are not creating a category; we are claiming a layer inside it.
- **Prefix caching is commodity, not moat.** vLLM/SGLang do it for free; providers discount cached reads 50–95%. Our intra-agent result is a *tie* (GAP-02). We only win when prefixes are **shared across divergent agents**, which is where our 2–9× (honest strong-baseline: 1.8–2.6×) lives.
- **Memory vendors own persistence and distribution.** Mem0 (~48k stars), Zep/Graphiti (~20k), Cognee ($7.5M seed, 70+ deployments), Letta (funded, MemGPT lineage) have ingestion, entity resolution, temporal facts, SDKs, and benchmarks we do not.
- **Compression research is catching our "bounded context" claim cheaply.** ReFold, TokenPilot, CliffCompaction, ACON all cut 26–90% of cost without a protocol, schema, or cartridge. TokenPilot explicitly optimizes for **cache continuity** — our differentiator is being absorbed into papers.
- **Our defensible wedge is narrower than the pitch:** cross-agent CoW prefix sharing + first-class negative knowledge + deterministic auditable packets, sold first to local vLLM operators with many role-divergent agents. Everything else is table stakes by 2027.

## Landscape at a glance

| Category | Players | Overlap with us | Our honest edge |
|---|---|---|---|
| Memory / persistence | Mem0, Zep/Graphiti, Cognee | Medium (state, not rendering) | Deterministic bounded packet; we can consume their stores |
| Agent memory runtime | Letta / MemGPT | High (context management) | Framework-agnostic, cross-agent sharing; they own the agent loop |
| Agent OS / academic | AIOS, MemOS, MemoryOS | High (conceptual) | Shipped KV-level measurements; they are schedulers/abstractions |
| KV cache / serving | LMCache, CacheBlend, vLLM APC, SGLang, CacheScout | Very high (the substrate) | Semantic packet + CoW fork; they optimize blocks, not meaning |
| Compression / folding | ReFold, AgentFold, LLMLingua, Complexity Trap, ACON, TokenPilot, CliffCompaction | Very high (the symptom) | Shared immutable prefix + negative knowledge + audit; most discard |
| Provider caching | OpenAI, Anthropic, Google | High (free intra-agent savings) | Cross-provider, cross-agent, semantic; they only cache identical prefixes |
| "Context compiler" framing | Google ADK, LangChain Deep Agents, Aegis, Contextual AI, Impetus | Very high (narrative) | Open protocol + KV-level evidence + negative knowledge; they have distribution |

---

## 1. Memory / persistence

| Player | What it is | (a) Better than us today | (b) How we beat or complement |
|---|---|---|---|
| **Mem0** | OSS + managed per-user memory layer; LLM-extracted atomic facts, vector + optional graph, self-editing/conflict resolution. Claims 91.6 LoCoMo, 93.4 LongMemEval (Apr 2026), ~6.9K tokens/retrieval. | Real ingestion, dedupe, multi-tenant API, SDKs, ~48k stars, production users; benchmarked accuracy we cannot match. | Do not rebuild memory. Ingest Mem0 facts into Tier 1 and compile them into a bounded, cache-aligned packet; sell "Mem0 tells you *what* is true, CF decides *what this agent sees now*." |
| **Zep / Graphiti** | Temporal knowledge graph of entities/facts with validity windows and provenance; Zep Cloud (SOC 2, HIPAA) + OSS Graphiti. ~1.6K tokens/retrieval on LongMemEval, 71.2% accuracy. | Temporal validity, compliance posture, graph traversal, managed cloud; far more mature enterprise story. | Complement: use Graphiti as the truth store, CF as the renderer. Beat on token budget + KV sharing for concurrent agents; their retrieval is per-query, ours is per-fleet. |
| **Cognee** | Graph-native memory engine (relational + vector + graph), self-improving "Memify" pipeline, 14 retrieval modes, 70+ deployments, ~$7.5M seed. | Multi-source ingestion, graph reasoning, integrations (LangGraph, CrewAI, MCP), published benchmarks and funding. | Complement at the boundary: their graph answers "what relates to what"; our packet answers "what fits in 3K tokens and stays cache-stable." Compete only on latency/VRAM for multi-agent fan-out. |

**Read:** this category is a partner channel, not a head-on fight. Our weakness is that we have no durable memory product; their weakness is that they render unbounded, per-query context and do not control KV layout.

---

## 2. Agent-managed memory runtime

| Player | What it is | (a) Better than us today | (b) How we beat or complement |
|---|---|---|---|
| **Letta / MemGPT** | Stateful agent runtime from the MemGPT lineage: typed memory blocks, recall/archival tiers, sleep-time "dream" subagents, git-backed MemFS, Context Constitution, desktop/cloud. Funded, model-agnostic. | Owns the agent loop, memory editing, continual learning, distribution, and a credible research brand. Their memory is *self-managed*; ours is compiled. | Complement: run CF underneath Letta as the packet/prefix layer for forked subagents. Beat on cross-agent determinism and KV sharing; do not fight for the agent runtime. |

**Read:** Letta is the most philosophically aligned competitor and the most likely to build our feature. Move fast on the framework-agnostic protocol and get an adapter into their ecosystem before they ship a native equivalent.

---

## 3. Academic agent OS / memory

| Player | What it is | (a) Better than us today | (b) How we beat or complement |
|---|---|---|---|
| **AIOS** | LLM agent operating system kernel: agent scheduler, context manager (snapshot/restore), memory manager (RAM + persistent two-tier), LLM system calls, SDK. COLM 2025. | Systems-level scheduling and isolation, academic credibility, a real kernel abstraction beyond our packet. | Coexist: AIOS schedules agents; CF decides each agent's context. Beat on concrete KV/VRAM measurements and semantic sharing; AIOS has no negative knowledge or cryptographic packet. |
| **MemOS** | "Memory OS" treating memory as a managed resource: MemCube units, plaintext/activation/parameter memory, scheduling, permissions, provenance. | Unifies parameter-level and activation-level memory; stronger long-term governance story; larger research team. | Complement: MemOS is storage/scheduling; CF is the compile-and-render step. Beat on deployed evidence and cache alignment. Also track **MemoryOS** (EMNLP 2025; hierarchical short/mid/long-term, LoCoMo +49% F1) as the benchmark to beat on conversational memory. |

**Read:** academic work validates the "context as OS" metaphor, which helps our narrative but erodes novelty. Cite them, integrate their benchmarks, and compete on reproducibility and shipping.

---

## 4. KV-cache / inference

| Player | What it is | (a) Better than us today | (b) How we beat or complement |
|---|---|---|---|
| **LMCache** | Open-source KV cache layer for vLLM/SGLang: CPU/disk offload, cross-query prefix reuse, PD disaggregation; up to 15× throughput, ≥2× lower latency. Ships CacheBlend. | Actual KV control, engine integration, production adoption, and multi-tier storage. We only *align* tokens; they move tensors. | Integrate: emit LMCache-compatible cache keys/pinning hints so our Tier 0/1 prefix is retained. Beat on semantic dedupe across role-divergent agents, where block hashing cannot help. |
| **CacheBlend** | Selective recompute of a small fraction of tokens to reuse non-prefix chunks; 2.2–3.3× lower TTFT, 2.8–5× throughput vs prefix caching. | Reuses *non-prefix* context, which our append-only packet deliberately avoids; strong RAG/multi-doc results. | Complement: CacheBlend handles retrieved chunks; CF guarantees a stable shared prefix so blending is unnecessary for project truth. Beat on zero-recompute determinism. |
| **vLLM automatic prefix caching** | Hash-based block cache keyed on tokens + prefix; free, output-identical, `cache_salt` isolation. | Ubiquitous, free, already deployed; neutralizes our intra-agent claim (GAP-02). | Ride it: our value is making many agents share the *same* prefix, not making one agent cheaper. If we cannot beat APC on a fleet workload, we have no product. |
| **SGLang RadixAttention** | Radix-tree KV reuse across requests, strong for shared system prompts and few-shot prefixes. | Mature, fast, widely used; better raw serving performance than a protocol layer. | Complement: CF emits radix-friendly, byte-stable prefixes; benchmark against SGLang as the baseline, not just vLLM. |
| **CacheScout** | Agent-aware KV runtime on vLLM: learns agent execution transitions to predict next agent, guides eviction/prefetch; +10–18pp hit rate, −18–45% TTFT, +57% throughput, <25KB state. | Solves the exact failure we hand-wave: our shared prefix gets LRU-evicted between agent invocations. Agent-aware, no workflow graph required. | Integrate or clone the idea: CF should ship eviction hints (or a CacheScout adapter). Beat on semantic equivalence and negative knowledge; combine for a stronger story: "compile the prefix, then protect it." |

**Read:** this is both our substrate and our biggest threat. The serving layer is learning agent semantics; if CF only reorders text, LMCache/CacheScout can commoditize it.

---

## 5. Context folding / compression

| Player | What it is | (a) Better than us today | (b) How we beat or complement |
|---|---|---|---|
| **ReFold** | Training-free reversible rendering layer: dedupe + fold completed turns, chunked rewrites, restore on demand. 2.5× fewer tokens, ~50% KV memory, 92% fewer forced compactions, 1.7× speed, 3.4× cost cut. | Reversible, plug-and-play, no cartridge/protocol, measured on five long-horizon benchmarks and two frontier LLMs. | Beat on cross-agent sharing and shared negative knowledge; adopt reversible folding inside Tier 3. Their per-session rendering cannot share a prefix across agents. |
| **AgentFold** | Proactive multi-scale folding for long-horizon web agents, with look-back over multi-step subtrajectories. | Learned/agentic folding granularity; strong web-agent results; no fixed schema. | Complement: CF can call it as a Tier 3 operator. Beat on determinism and audit; their folding is a policy, ours is a contract. |
| **LLMLingua / LongLLMLingua / LLMLingua-2** | Token-level prompt compression via perplexity filtering or a distilled classifier; widely used, task-generic, high compression ratios. | Mature, cheap, model-agnostic, huge adoption; compresses any prompt without a compiler. | Beat on *cache safety* and semantic guarantees: token pruning mutates prefixes and invalidates KV; our folding happens at epoch boundaries with pointers. Complement for one-shot documents. |
| **"The Complexity Trap"** | Shows simple observation masking matches LLM summarization for agent context, at lower cost; a strong baseline paper. | Cheap, robust, hard to beat; sets the honest baseline our evals must include. | Use it as the baseline we must beat, not hide from. Our claim must be "better than masking *at fleet scale*," not "better than naive broadcast." |
| **ACON** | ICML 2026; optimizes natural-language compression guidelines from full-vs-compressed success/failure trajectories, distills compressor; 26–54% peak token cut, up to 46% small-model gain. | Learns what to keep from failures; directly addresses weak-model fragility; strong benchmarks (AppWorld, OfficeBench). | Complement: ACON is a great Tier 3 policy. Beat on first-class negative knowledge (we store the failure itself, not just a guideline) and cross-agent reuse. |
| **TokenPilot** | Cache-efficient context management: ingestion-aware compaction to stabilize prefixes + lifecycle-aware eviction; 56–87% cost cuts on PinchBench/Claw-Eval. EMNLP 2026 Findings. | Explicitly targets the sparsity vs cache-continuity trade-off — our exact thesis — with strong cost numbers and a harness. | This is the closest paper to our pitch. Beat on multi-agent CoW and semantics; adopt its ingestion gate + eviction schedule. If we cannot differentiate, we become a worse TokenPilot. |
| **CliffCompaction** | Autocompaction proxy that only truncates/drops (never rewrites), 16–32K thresholds; 80% lower cache-read cost, ~50% total cost, SWE-bench parity with GLM-5.1/Kimi K2.6; SOTA KernelBench. | Simple, transparent, cheap, and validated on hard coding benchmarks with real providers. | Beat on cross-agent sharing and persistent truth; their per-session compaction discards history by design. Adopt "never compact a compaction" as a Tier 1 rule. |

**Read:** the research frontier is solving context cost *without* our protocol. Our remaining differentiator is not compression ratio — it is **shared, deterministic, auditable state across agents**.

---

## 6. Provider prompt caching

| Player | What it is | (a) Better than us today | (b) How we beat or complement |
|---|---|---|---|
| **OpenAI** | Automatic prefix caching, up to ~95% cached-input discount, 1,024-token minimum (GPT-5.6+), 1.25× cache-write, dashboard + cache diagnostics. | Zero integration, huge discount, observability, default-on. Makes intra-agent context cheap. | Complement: our packet is a *better cache key* (stable Tier 0/1, growing tail). Beat where agents diverge: identical-prefix caching cannot share across different roles/tasks. |
| **Anthropic** | Explicit `cache_control` breakpoints, 5-min default / 1-hour TTL (2× write), reads ~0.1×; memory tool + context editing. | Fine-grained control, long TTL, memory tool, strong coding-agent adoption (Claude Code). | Complement: emit Anthropic-friendly breakpoints and a 1-hour-safe packet layout. Beat on cross-provider portability and semantic negative knowledge; their memory tool is per-agent file storage, not shared truth. |
| **Google** | Implicit caching on Gemini 2.5+ (~90% discount, 2–4K minimum), explicit caches with TTL (default 1h), explicit caching not supported on Gemini 4 Argon+. | Cheapest cached reads at scale, implicit by default, multimodal, ADK distribution. | Complement: target Gemini implicit caching with a stable prefix; beat on provider-neutral compilation and fleet-level sharing. Google ADK is also a framing competitor (below). |

**Read:** providers are collapsing the value of "same prefix, cheaper." Our pitch must be *different prefixes, shared truth* — not "we save tokens on repeated prompts."

---

## 7. "Context engineering" / "context compiler" players

| Player | What it is | (a) Better than us today | (b) How we beat or complement |
|---|---|---|---|
| **Google ADK** | Open-source agent framework whose stated thesis is "context is a compiled view": sessions/memory/artifacts as sources, processors as compiler pipeline, minimum-scope context per call. | Google distribution, production hardening, explicit compiler abstraction, multi-agent semantics — and it is free. | Beat on open protocol + KV evidence + negative knowledge. Complement: provide a CPP adapter for ADK; do not compete on framework features. This is the most dangerous narrative competitor. |
| **LangChain Deep Agents** | Default harness with filesystem offload, summarization at 85%, subagent isolation, memory middleware, prompt-caching middleware; massive mindshare. | Distribution, ergonomics, real coding-agent usage, filesystem recovery, subagents. | Complement: CF as a middleware/backend for "shared project truth" across subagents. Beat on cross-agent prefix sharing and deterministic packets; they summarize per-thread. |
| **Aegis** | MCP server billing itself as a "DAG-based deterministic context compiler for AI coding agents": compile exactly which docs an agent needs from target files, no search/ranking; ~114 stars. | Literally our "deterministic compiler" framing, zero-GPU, easy install, human-approved architecture rules. | Beat on scale: DAG doc selection does not bound tokens, share KV, or handle negative knowledge. Complement/acquire: their deterministic dependency graph is a good Tier 0 indexer. |
| **Contextual AI** | Enterprise "unified context layer" + Agent Composer for engineering documents; funded, enterprise sales. | Enterprise distribution, ingestion/RAG quality, sales motion, trust. | Different market (enterprise docs vs local agent fleets). Complement if they need bounded packets; otherwise avoid head-on enterprise RAG. |
| **Impetus (CEDL / Leap AI)** | Services-led "Context Engineering Delivery Lifecycle": governance, ontologies, context pipelines for enterprises. | Enterprise relationships, delivery capacity, budget. | Not a product competitor; a channel/partner for regulated deployments where our audit receipt matters. |
| **contextengineering.ai / long tail** | Small tools and consultancies using the label. | Speed and marketing. | Noise; ignore except as evidence the term is commoditizing. |

**Read:** the narrative is being claimed by players with distribution. We should stop competing on the word "compiler" and compete on the *artifact*: a portable, signed packet that any framework can mount.

---

## Where we can win

1. **Cross-agent CoW sharing under role divergence.** 9.5× on role-divergent children is our strongest number (unequal-information caveat: the B0 children get rotations of the same raw context with no quality gate — VERIFICATION-REPORT N2). Neither prefix caching (different prompts), nor memory layers (per-query retrieval), nor compaction (per-session) can share a byte-identical Tier 0/1 prefix across 50 different agents.
2. **Negative knowledge as a first-class, shared artifact.** 16.7% → 0% repeated failures in the B6.5 probe (n=24, weakness caveat N14); the current demo set is directionally consistent at 3B and a null at 1.5B (`docs/engineering/DEMO.md`). ReFold/CliffCompaction discard; Mem0/Zep store facts, not falsifications; ACON learns guidelines, not a ledger. This is genuinely novel and demo-able in five minutes.
3. **Deterministic, replayable, auditable packets.** Cryptographic receipts + pinned determinism is enterprise/regulated value that probabilistic summarizers cannot offer. No competitor in this list ships this.
4. **Local-first vLLM/LMCache economics.** 20× lower KV at 16 agents (vs unshared raw broadcast; only ~2× vs a shared raw prefix) on an 8GB RTX 5070 is a real, differentiated on-prem story for sovereign/air-gapped teams who cannot use provider caching.
5. **Framework-agnostic protocol.** Google ADK and LangChain are frameworks; Mem0/Letta are products. A neutral CPP schema can become the interchange format for "what an agent sees," the way AGENTS.md became the context-file convention.

## Where we are weaker (blunt)

- **Prefix caching is the competitor, and it is free.** Intra-agent is a tie. Providers discount cached reads 50–95%. If the buyer's problem is "one agent, repeated prompt," we lose to a config flag.
- **Our headline 2–9× is baseline-sensitive.** Against a strong `B0_tail`, the honest number is 1.8–2.6×. We must never compare only to naive broadcast.
- **Weak-model dependence is unresolved.** GAP-08 model-tier arbitrage is undemonstrated; the terse packet format failed on a weak model; ACON shows compression can *hurt* small models without learned guidelines.
- **Quality evidence is thin.** The B4 task is trivially easy (copy a value, 572 tokens), with no long-context reasoning, no error bars, and synthetic workloads. We have no SWE-bench/Terminal-Bench-class result.
- **No memory/persistence product.** We do not ingest, dedupe, resolve entities, track temporal validity, or personalize. Mem0/Zep/Cognee do, with benchmarks and SDKs.
- **No KV-engine control.** We align tokens but do not pin, evict, prefetch, or move tensors. CacheScout/LMCache can invalidate our core claim with an LRU policy.
- **Adoption and team surface are tiny.** No integrations ecosystem, no funding signal, no community. Google/LangChain/Mem0/Letta can out-ship and out-market us.
- **Protocol complexity is a adoption tax.** Schema + canonicalization + signing + determinism is heavier than `pip install mem0ai` or one middleware file. Every invariant must pay for itself in a demo.
- **The 20.2× KV (vs unshared raw broadcast) and 8.23× throughput numbers (16 agents; 43.25 vs 5.26 req/s, 2026-10-17 re-run) are single-node, 8GB, workload-specific.** They will not replicate unchanged on a 70B model or a cloud fleet; we need error bars and cross-engine results.

## Concrete moves (ranked)

1. **Reframe the benchmark, then publish it.** Head-to-head on one public repo + 8 role-divergent agents: naive broadcast, strong `B0_tail`, vLLM APC only, LMCache, CacheScout-style pinning, TokenPilot-style compaction, and CF. Pre-register metrics (cost/agent, TTFT, KV peak, task success, failure repeats). **Success: ≥2× vs the best cache-only baseline, not vs naive.**
2. **Ship a 10-minute adapter + demo.** LangGraph or Claude Code adapter that compiles `AGENTS.md` + repo into a `.cfx` cartridge and mounts it for N subagents. One command, one public repo, one before/after trace. Distribution beats architecture.
3. **Lead with negative knowledge, not compression.** Demo: agent A tries a dead-end API; agent B is hard-blocked; zero repeats, receipt attached. This is the only feature in the list no one else has. Compression is the commodity; failure memory is the hook.
4. **Integrate the KV layer instead of pretending to own it.** Emit LMCache-compatible keys and pin/eviction hints; add a CacheScout adapter; test under SGLang RadixAttention. "CF compiles the prefix; LMCache/CacheScout protects it" is a stronger, more credible story than a proprietary cache claim.
5. **Run one real long-horizon eval with a frontier and a small model.** SWE-bench Verified subset or Terminal-Bench 2.0, with ACON/CliffCompaction/observation-masking baselines. Publish quality parity at compression and a failure taxonomy. This closes GAP-22 and GAP-08 or kills the arbitrage thesis honestly.
6. **Position as a complement to memory vendors.** Build importers for Mem0/Zep/Cognee/Graphiti and a "bring your own truth store" path. Do not rebuild persistence; sell the renderer/compiler above it.
7. **Open the packet schema and ship a conformance suite.** Publish CPP v1.0 with golden packets, a validator, and a negative-knowledge extension. Seek adoption as an AGENTS.md-adjacent convention before Google or LangChain defines the de facto format.
8. **Monetize audit and policy, not tokens.** Enterprise RBAC, signed packet receipts, compliance evidence, and multi-tenant sidecar for regulated on-prem vLLM teams. Compression savings are easy to copy; audit trails and policy packs are not.

## One-line positioning

> Mem0/Zep/Letta remember; LMCache/CacheScout move KV blocks; ReFold/TokenPilot/CliffCompaction shrink one session. **Context Fabric compiles shared project truth into one signed, cache-stable packet that every agent can fork — and remembers what not to try again.**

---

### Sources (selected)

- Mem0 comparisons: mem0.ai/compare; Zep vs Mem0 (2026); Cognee memory-tool guides (2026).
- Agent-memory benchmark pilot: github.com/datapace-ai/agent-memory-benchmark (2026).
- Letta: letta.com, docs.letta.com/letta-agent/memory, sleep-time compute (2025–2026).
- AIOS: arXiv 2403.16971 (COLM 2025); AIOS docs. MemOS: arXiv 2507.03724. MemoryOS: arXiv 2506.06326 (EMNLP 2025).
- LMCache: arXiv 2510.09665; docs.lmcache.ai (CacheBlend). vLLM APC docs; SGLang RadixAttention.
- CacheScout: arXiv 2608.14624 (2026). ReFold: arXiv 2610.07863. AgentFold: arXiv 2510.24699.
- LLMLingua (2023–2024); The Complexity Trap: arXiv 2508.21433; ACON: arXiv 2510.00615 (ICML 2026).
- TokenPilot: arXiv 2606.17016 (EMNLP 2026 Findings). CliffCompaction: arXiv 2609.26779.
- OpenAI prompt caching docs (2026); Anthropic prompt caching docs (2026); Gemini API caching docs (2026).
- Google ADK context-stack blog (2025-12); LangChain Deep Agents context engineering docs (2026); Aegis (github.com/fuwasegu/aegis); Contextual AI Agent Composer (2026-01); Impetus CEDL (2026-08); Redis State of Context Engineering (2026).
