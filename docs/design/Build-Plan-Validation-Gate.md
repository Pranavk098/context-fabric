# 02 - Phased Build Plan and Go/No-Go Validation Gates

## Guiding Rule for Coding Agents
Never build ahead of the current phase's verification gate. Every phase produces an executable, deterministically testable vertical slice[cite: 37]. Do not introduce learned neural rerankers or LLM-based summarizers in Phases 0 through 3; prove the deterministic Agent MMU kernel first[cite: 21, 37, 38].

---

## Phase 0: Telemetry Harness and The "Redundancy Flamegraph" (Days 1-4)
**Goal:** Quantify the exact multi-agent token duplication and KV-cache thrashing problem on baseline workflows before enabling Context Fabric routing[cite: 37].

### Deliverables:
1. `protocol/models.py` and `protocol/canonical.py`: Complete Pydantic v2 schemas for `WorkflowEvent`, `ContextBlock`, `ContextPacket`, and `AgentOutcome` with deterministic `sha256` hashing[cite: 25, 26, 31, 37].
2. `evals/harness.py`: Interceptor that wraps standard LLM calls and records:
   - Total input tokens, cached prefix tokens, uncached dynamic tokens, and output tokens[cite: 3, 34].
   - Prefix hash continuity across turns (1.0 if prefix matches prior turn, 0.0 if busted).
   - Duplicate block ingestion ratio across sub-agents.
3. `evals/suites/doom_loop_25_agents/`: A reproducible 5-stage to 25-hop software bug-fix and refactoring workflow (Triage -> Diagnose -> Implement -> Audit -> Correct) with gold-standard dependency labels[cite: 30, 35, 37].

### Verification Gate 0:
Run Baselines `B0 (Full Broadcast)` and `B2 (Vector Top-k)` on the workflow and output a JSON/HTML telemetry report showing the baseline token explosion and failure modes[cite: 12, 34].

---

## Phase 1: The Deterministic Protocol Kernel and Cartridge Compiler (Days 5-10)
**Goal:** Build the append-only PostgreSQL event store, `.cfx` Zero-Token Cartridge generator, and deterministic 4-Tier Packet Compiler[cite: 36, 37].

### Deliverables:
1. `kernel/db/migrations/001_initial_fabric_schema.sql`: Full PostgreSQL DDL for events, blocks, relations, cartridges, manifests, packets, and receipts.
2. `kernel/cartridge/`: Tree-sitter AST parser + PageRank structural hologram builder that compiles a target repository into a 1,200-token Tier 0 `.cfx` Cartridge in under 500ms with zero LLM calls.
3. `router/compiler.py` + `adapters/raw_llm/`: Deterministic compiler that packages Tier 0 (Cartridge), Tier 1 (Ledger), Tier 2 (Swap Table), and Tier 3 (Working Register) and renders them to OpenAI, Anthropic, Gemini, and vLLM API calls with exact prompt-cache breakpoints[cite: 25, 37].

### Verification Gate 1:
100% unit test pass rate on canonical JSON hashing, byte-identical Tier 0/1 prefix rendering across different agent roles, and Ed25519 signature verification[cite: 24, 25, 37].

---

## Phase 2: The Agent MMU, Policy Engine, and Shadow Cortex (Days 11-18)
**Goal:** Enable the full 9-stage deterministic router, Shadow Cortex pre-execution probes, Negative Knowledge pruning, and Intra-Turn Epoch Folding[cite: 27, 37].

### Deliverables:
1. `router/policy_engine.py`: Enforce hard RBAC, trust domain isolation, valid-time freshness floors, and Auditor role-blindness (stripping Implementer persuasive observations during audit stage)[cite: 27, 30, 32].
2. `router/shadow_cortex.py`: Deterministic pre-execution probes (stack-trace line extractor, AST function resolver, `git diff` hunk slicer) executing in under 15ms.
3. `router/recall.py`, `router/dependency_closer.py`, `router/scorer.py`, `router/packer.py`: Hybrid recall (`pgvector` + `tsvector`), mandatory closure of `depends_on`, `contradicts`, and `negative_knowledge` blocks, cache-aware utility scoring, and MMR packing[cite: 26, 27, 28].
4. `kernel/mmu/epoch_folder.py` and `cow_forker.py`: Implement `EPOCH_FOLD` (k=3 steps append-only -> fold raw tool outputs into Tier 1 facts + Tier 2 `ctx://` pointers) and `COW_FORK` (sub-agent spawning with constant window size).

### GO / NO-GO GATE (End of Phase 2)[cite: 37]:
Run `CF-v1.0` against `B0`, `B2`, and `B5` on the 25-agent workflow and SWE-bench slice. Must demonstrate:
- >= 75% reduction in billed input tokens per successful task vs. `B0`[cite: 34, 35].
- >= 85% KV-cache prefix hit rate across intra-turn steps and sub-agent forks.
- Zero task-success regression vs. `B0` (and superior pass rate on small models via Model-Tier Arbitrage)[cite: 35, 42].

---

## Phase 3: Framework and Protocol Adapters + Parity Suite (Days 19-25)
**Goal:** Prove zero-rewrite integration across major orchestration frameworks and protocols[cite: 31, 35, 37, 42].

### Deliverables:
1. `adapters/langgraph/`: Drop-in `ContextFabricStateGraph` wrapper and tool-node interceptor[cite: 9, 37].
2. `adapters/crewai/` and `adapters/autogen/`: Role and task adapters[cite: 9, 20, 37].
3. `adapters/mcp_server/` and `adapters/a2a_bridge/`: MCP server exposing `ctx://` resources and `expand_packet` tool; A2A v1.0 task/artifact mapper[cite: 10, 31, 37].
4. `adapters/lmcache_driver/`: Local GPU `vLLM`/`LMCache` 256-token page alignment and pin/unpin integration.

### Verification Gate 3 (Semantic Parity Test)[cite: 31, 35, 37]:
Execute the identical logical Creator -> Auditor -> Corrector workflow across Raw API, LangGraph, and CrewAI adapters. Assert that pre-render logical `packet_id` hashes and policy traces are 100% identical across all three frameworks[cite: 21, 25, 35].

---

## Phase 4: Sidecar Daemon, Inspector UI, and Adversarial Hardening (Days 26-32)
**Goal:** Package Context Fabric as a production sidecar with an executive visual demo and pass the adversarial security certification[cite: 32, 35, 36, 37].

### Deliverables:
1. `services/api/`: FastAPI sidecar implementing all 9 REST endpoints with idempotency keys[cite: 36].
2. `services/inspector_ui/`: Web dashboard showing real-time token savings, 4-Tier memory map visualizer, KV-cache hit meter, and clickable "Why Included / Why Excluded" block receipts[cite: 11, 32, 42].
3. `evals/suites/adversarial_policy/`: Automated test suite verifying 0% bypass on prompt injection in untrusted source blocks, cross-project leakage, stale-fact supersession, and malicious agent write-backs[cite: 32, 35].