================================================================================
NORMATIVE ARCHITECTURE SPECIFICATION | VERSION 1.0 (SUPERSEDES CPP v0.1)
================================================================================

# CONTEXT FABRIC & CONTEXT PACKET PROTOCOL (CPP v1.0)
## System Architecture, Virtual Memory Tiers, Syscalls, Scoring, and Governance

> **Status note (2026-10-18):** design of record, not a status report. What is built and the
> spec-vs-code deltas live in `../engineering/IMPLEMENTATION-STATUS.md` and
> `../engineering/ROADMAP-DECISIONS.md`. Known deltas as of this date: policy enforces
> lifecycle/RBAC/validity/trust-domain/purpose (purpose is an optional compile-time parameter) and freshness gates implemented;
> the >85% KV-reuse invariant is an empirical target (intra-agent measured 60–74%);
> CoW is implemented at the packet/memory-state level (`kernel/epochs.py`, `kernel/mmu/`),
> not as KV-page forking; packet signing is optional in the compiler.

### 1. ARCHITECTURAL INVARIANTS (THE 10 NON-NEGOTIABLES)

1. **Canonical Outside, Register Inside:** Shared truth lives in the Fabric's append-only Knowledge Plane[cite: 21, 22]. Each agent's context window is a bounded L1 register. The v1.0 default `hard_total_tokens = 4,000`, split into a 3,000-token dynamic working set (Tier 1 + Tier 2 + Tier 3) plus a 600-token static Tier 0 cartridge and a 400-token reserve. See `docs/decisions/ADR-0001`.[cite: 21].
2. **Least-Context Disclosure by Default:** Every context block is denied by default and must pass hard RBAC, purpose, trust-domain, and freshness gates *before* similarity or utility scoring[cite: 21, 27, 32, 38].
3. **Strict 4-Tier Prefix Stratification:** Packets MUST serialize from lowest-mutation frequency (Tier 0 Cartridge) to highest-mutation frequency (Tier 3 Ephemeral Register) to guarantee >85% KV-cache reuse across turns and sub-agents. **Growth-at-tail rule (audit GAP-19):** anything that grows within an epoch (Tier 3 observations) MUST render at the very tail; tiers that mutate at fold boundaries (Tier 1/2) MUST NOT precede higher-mutation tiers in a way that displaces an already-cached shared prefix. Appending to a tier must never shift the byte offset of an earlier shared section.
4. **Zero-Token Cold Boot:** Agents MUST NOT spend LLM tokens discovering static project structure. Structural maps (`.cfx` Cartridges) and deterministic Shadow Cortex probes execute outside the LLM[cite: 23].
5. **$k$-Step Append-Only Micro-Epochs:** Intra-turn tool loops MUST append strictly to Tier 3 for $k$ steps before triggering an `EPOCH_FOLD`, preventing mid-turn causal KV-cache invalidation.
6. **Reversible Eviction (Never Lossy Amnesia):** Evicted raw tool outputs and large artifacts MUST be replaced with addressable `ctx://` swap pointers in Tier 2, expandable on demand via `PAGE_FAULT_EXPAND`[cite: 26, 28].
7. **Copy-on-Write (CoW) Sub-Agent Spawning:** Child agents MUST share immutable Tier 0 and Tier 1 pages with parent agents and discard their ephemeral Tier 3 scratchpad upon returning a verified contract[cite: 24, 29].
8. **First-Class Negative Knowledge:** Falsified hypotheses, failed patches, and broken assumptions MUST be recorded as `negative_knowledge` blocks in Tier 1 so no downstream agent repeats a dead end.
9. **Deterministic Core, Learned Edge:** Policy enforcement, dependency closure, epoch folding, and budget caps are 100% deterministic[cite: 21, 38]. Learned rerankers/compressors are optional plugins behind feature gates[cite: 20, 27, 37, 38].
10. **Cryptographic Replayability:** Every compiled packet is immutable, content-hashed (`sha256`), signable, and linked to an explainable route trace[cite: 21, 24, 25, 32].

---

### 2. FOUR-PLANE REFERENCE ARCHITECTURE

* **PLANE 1: EXPERIENCE & RUNTIME PLANE**
  External agent frameworks (LangGraph, CrewAI, AutoGen, OpenAI Agents SDK), MCP hosts/servers, A2A peers, and LLM inference endpoints (Cloud APIs, vLLM, SGLang + LMCache)[cite: 9, 10, 22].
* **PLANE 2: DELIVERY & MMU INTERCEPTOR PLANE**
  Framework Adapters (`FabricAdapter`), Intra-Turn Step Interceptors, Push Subscription Broker, Pull Expansion Handler (`PAGE_FAULT_EXPAND`), and Receipt Collector[cite: 22, 23, 31].
* **PLANE 3: DECISION & EXOCORTEX PLANE**
  Task Interpreter, Shadow Cortex (deterministic AST/git/log probes), Hard Policy Engine, Hybrid Candidate Retriever, Dependency & Contradiction Closer, Cache-Aware Utility Scorer, 4-Tier Budget Allocator, and Packet Compiler/Signer[cite: 22, 23, 27].
* **PLANE 4: KNOWLEDGE & CARTRIDGE PLANE**
  Append-Only Canonical Event Log (PostgreSQL), Derived Block Store (`pgvector` + `tsvector` BM25), Bi-Temporal Contradiction & Dependency Graph, `.cfx` Cartridge Compiler (Tree-sitter + PageRank), and Manifest Registry[cite: 22, 23, 36, 38].

---

### 3. THE 4-TIER VIRTUAL MEMORY PACKET LAYOUT (CPP v1.0)

Every compiled `ContextPacket` renders into four contiguous, hash-aligned memory tiers:

* **TIER 0: `cartridge_mount` (Default Budget: 15% | ~600 Tokens | 100% Shared KV Cache)**
  Contains the pre-compiled `.cfx` Project Hologram (topological module map, core data schemas, global architectural invariants, and immutable system instructions). Byte-identical across every agent in the project cohort.
* **TIER 1: `ancestral_ledger` (Default Budget: 25% | ~1,000 Tokens | 95% Shared KV Cache)**
  Contains settled cross-agent `decision` blocks, active interface contracts, unresolved contradiction alerts, and `negative_knowledge` blocks (the Graveyard of Failed Attempts). Updated append-only at workflow phase or epoch boundaries.
* **TIER 2: `swap_directory` (Default Budget: 5% | ~200 Tokens | Address Table)**
  A compact table of `ctx://` pointers representing evicted prior-epoch tool outputs, full source files, and large logs, each with a 1-line semantic signature, byte/token size, and SHA-256 version hash[cite: 26, 28].
* **TIER 3: `working_register` (Default Budget: 45% | ~1,800 Tokens | Ephemeral Agent Tail)**
  Contains the role-specific `instruction` frame[cite: 24, 28], pre-executed `shadow_probe` slices (exact AST functions, diffs, failing lines), active step observations (append-only for $k$ steps), and the final **Recency Salience Anchor** (re-stating the exact output contract and prohibitory constraints at the very bottom of the prompt to beat "Lost in the Middle").
* **SAFETY RESERVE (Default Budget: 10% | ~400 Tokens)**
  Reserved for tokenizer variance and intra-epoch delta growth[cite: 28].

> **Budget note (ADR-0001):** The percentages are normative and scale with the manifest's
> `hard_total_tokens`. The v1.0 default base is **4,000** tokens, yielding the absolute
> figures above. The "~3,000 dynamic tokens" headline refers to Tier 1 + Tier 2 + Tier 3
> (1,000 + 200 + 1,800); Tier 0 (600) is a shared, cache-resident cartridge. Retire any
> other absolute packet-size figure in the documents.

---

### 4. CACHE-AWARE & EPISTEMIC UTILITY SCORING MODEL

After hard policy filtering (`policy_allow == true`, `freshness >= task_floor`, `trust_domain == match`)[cite: 21, 27], candidate blocks $i \in \mathcal{C}$ are scored deterministically:

$$U(i) = w_r R(i, t) + w_\rho \text{Role}(i, a) + w_s \text{Stage}(i, w) + w_f \text{Fresh}(i) + w_a \text{Auth}(i) + w_d \text{Dep}(i) + w_c \text{Conflict}(i) + w_p \text{Prune}(i) - w_t \text{EffCost}(i) - w_{\text{risk}} \text{Risk}(i)$$

Where:
* $\text{Prune}(i) \in [0, 1]$ measures **Counterfactual Exploration Savings** (highest for `negative_knowledge` and `shadow_probe` blocks that prevent redundant tool calls).
* $\text{EffCost}(i) = \frac{\text{tokens}(i)}{B_{\text{tier}}} \cdot \left(1 - \gamma_{\text{cache}} \cdot \mathbb{I}_{\text{cached}}(i)\right)$ discounts the token penalty of blocks already resident in Tier 0/1 warm KV cache by the provider's cache discount factor $\gamma_{\text{cache}} \in [0.5, 0.9]$.

---

### 5. DEGRADED MODES, CONFLICT RESOLUTION & SECURITY CONTROLS

* **Contradictions Are Never Silently Merged:** If Block $A$ and Block $B$ have a `contradicts` edge and neither has an explicit `supersedes` decision block, the Dependency Closer MUST include *both* blocks plus a conflict flag in Tier 1/3[cite: 4, 26, 27, 30, 33].
* **Role-Scoped Blindness (Anti-Bias Auditing):** In verification stages (`role == "auditor"`), the Policy Engine strips the Implementer's persuasive narrative rationale (`kind == "observation"`) while preserving the raw diff (`artifact`), test outputs (`fact`), and requirements (`instruction`)[cite: 26, 30].
* **Fail-Closed Degraded Modes:**
  * *Embeddings Down:* Fall back to BM25 (`tsvector`) + AST symbol graph + explicit refs (`retrieval_degraded`)[cite: 33].
  * *Shadow Cortex Timeout (>25ms):* Emit packet without `shadow_probe` blocks (`probe_skipped`).
  * *Policy Engine Down:* Fail closed; emit only static template-safe instructions (`policy_degraded`)[cite: 33].
================================================================================