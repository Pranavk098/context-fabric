# 01 — Repository Structure & Tech Stack Specification

## 1. Core Technology Choices (Enforced for Phase 0–4)
* **Primary Language:** Python 3.12+ (strict `mypy` type checking, `pydantic>=2.9` for zero-overhead C-compiled schema validation, `fastapi` + `uvicorn` for the sidecar daemon)[cite: 36, 37].
* **Canonical Storage & Index Engine:** **PostgreSQL 16+** with extensions:
  * `pgvector` (HNSW indexing for dense semantic embeddings)[cite: 22, 38].
  * Native `tsvector` + GIN indexes (BM25/lexical exact-identifier search)[cite: 22, 27].
  * Recursive CTEs on `block_relations` table (for dependency closure, supersession chains, and contradiction edges—no external Graph DB required in v1.0)[cite: 22, 26, 36, 38].
* **Deterministic AST & Cartridge Compiler:** `tree-sitter` + `tree-sitter-languages` (for multi-language AST symbol export/import extraction in <10ms) + `networkx` (for in-memory Personalized PageRank over symbol graphs).
* **Tokenization & Hashing:** `tiktoken` (fast token counting) + standard library `hashlib.sha256` + `cryptography` (`Ed25519` packet signing)[cite: 25, 32].
* **Observability:** `opentelemetry-api` / `opentelemetry-sdk` for route, compile, fold, and delivery spans[cite: 31].

---

## 2. Monorepo Directory Tree (`context-fabric/`)

```text
context-fabric/
├── pyproject.toml                     # Poetry / uv workspace configuration
├── docker-compose.yml                 # PostgreSQL 16 (pgvector) + Context Fabric Sidecar
├── Makefile                           # Format, lint, typecheck, migrate, test, benchmark
│
├── protocol/                          # [MODULE 1] Zero-dependency schemas & canonicalization
│   ├── __init__.py
│   ├── schemas/                       # Exported JSON Schema Draft 2020-12 files[cite: 31, 37]
│   │   ├── cpp_envelope_v1.json
│   │   ├── context_block_v1.json
│   │   ├── agent_manifest_v1.json
│   │   └── workflow_event_v1.json
│   ├── models.py                      # Pydantic v2 immutable models for Packets, Blocks, Events
│   ├── canonical.py                   # RFC 8785 JCS canonical JSON serializer & sha256 hasher[cite: 25, 37]
│   └── signing.py                     # Ed25519 manifest signing and verification[cite: 22, 25, 32]
│
├── kernel/                            # [MODULE 2] Event Log, Storage, Cartridges & MMU State[cite: 37]
│   ├── __init__.py
│   ├── db/
│   │   ├── migrations/                # SQL migrations (001_initial_fabric_schema.sql)
│   │   ├── connection.py              # AsyncPG connection pool & watermark transactions[cite: 23, 33]
│   │   └── repositories.py            # EventRepository, BlockRepository, ManifestRepository
│   ├── ingestion.py                   # Idempotent event ingestion, deduplication, quarantine[cite: 23, 32]
│   ├── cartridge/                     # Zero-Token Cold Boot (.cfx) compiler
│   │   ├── ast_extractor.py           # Tree-sitter symbol export/import parser
│   │   ├── pagerank_map.py            # Structural hologram generator (Tier 0 builder)
│   │   └── serializer.py              # .cfx bundle packer & KV-page aligner
│   └── mmu/                           # Agent Virtual Memory Lifecycle handlers
│       ├── epoch_folder.py            # k-Step Append-Only micro-epoch folder (EPOCH_FOLD)
│       ├── cow_forker.py              # Copy-on-Write sub-agent packet spawning (COW_FORK)
│       └── swap_manager.py            # Tier 2 ctx:// pointer table & PAGE_FAULT_EXPAND[cite: 28, 36]
│
├── router/                            # [MODULE 3] Policy, Shadow Cortex, Recall, Scoring & Packing[cite: 37]
│   ├── __init__.py
│   ├── interpreter.py                 # Maps workflow event -> TaskSnapshot & budget profile[cite: 23, 27]
│   ├── shadow_cortex.py               # <15ms deterministic pre-execution probes (git, AST, stacktrace)
│   ├── policy_engine.py               # Deterministic RBAC, purpose, freshness & role-blindness gates[cite: 23, 27, 32]
│   ├── recall.py                      # Hybrid lexical (tsvector) + vector (pgvector) + symbol graph recall[cite: 23, 27]
│   ├── dependency_closer.py           # Closes depends_on, supersedes, contradicts, and negative_knowledge[cite: 26, 27]
│   ├── scorer.py                      # Cache-aware & counterfactual-pruning deterministic scorer[cite: 27]
│   ├── packer.py                      # 4-Tier budget allocator, MMR diversity selector, U-curve placer[cite: 23, 27, 28]
│   └── compiler.py                    # Orchestrates stages 0-8, renders profiles, emits route trace[cite: 23, 25, 27]
│
├── adapters/                          # [MODULE 4] Runtime & Protocol Integrations[cite: 31, 37]
│   ├── __init__.py
│   ├── base.py                        # Abstract FabricAdapter interface & conformance test mixin[cite: 31]
│   ├── raw_llm/                       # OpenAI / Anthropic / Gemini / vLLM raw API wrapper (Prompt + Cache hints)[cite: 25, 37]
│   ├── langgraph/                     # StateGraph node wrapper, checkpoint hook & intra-turn tool interceptor[cite: 9, 20, 37]
│   ├── crewai/                        # CrewAI Task/Agent context injection & step callback hook[cite: 9, 20]
│   ├── autogen/                       # ConversableAgent message filter & handoff compiler[cite: 9, 20, 37]
│   ├── mcp_server/                    # Exposes ctx:// lookup & packet expansion as MCP resources/tools[cite: 10, 31, 37]
│   ├── a2a_bridge/                    # Maps CPP packets to A2A v1.0 Tasks, Messages & Artifacts[cite: 10, 25, 31, 37]
│   └── lmcache_driver/                # Emits 256-token page hashes & pin/unpin calls to local vLLM/LMCache
│
├── services/                          # [MODULE 5] Sidecar Daemon & Operational Workers[cite: 36, 37]
│   ├── api/                           # FastAPI REST & WebSocket server (/events, /packets:compile, etc.)[cite: 36]
│   ├── broker.py                      # Push subscription delivery broker & idempotent retries[cite: 23, 33, 36]
│   └── inspector_ui/                  # Lightweight FastAPI + HTMX/React Packet Receipt & Diff Inspector
│
├── evals/                             # [MODULE 6] Falsifiable Benchmarks & Telemetry Suite[cite: 34, 35, 37]
│   ├── harness.py                     # Unified runner recording tokens, KV-cache hits, latency, accuracy
│   ├── baselines/                     # B0 (Full Broadcast), B2 (Vector Top-k), B4 (Mem0), B5 (Naive Masking)[cite: 12, 34]
│   ├── suites/
│   │   ├── doom_loop_25_agents/       # 25-agent recursive software migration & bug-fix benchmark
│   │   ├── swe_tier_arbitrage/        # Frontier planner + Small model worker benchmark[cite: 35, 37]
│   │   ├── longmem_temporal/          # LongMemEval staleness & supersession suite[cite: 12, 35]
│   │   └── adversarial_policy/        # Prompt injection, cross-role leakage & poisoned summary suite[cite: 12, 32, 35]
│   └── reports/                       # Automated Pareto frontier plots & executive telemetry tables
│
└── docs/                              # Architecture Decision Records (ADRs) & Engineering Specs[cite: 37]
    └── specs/