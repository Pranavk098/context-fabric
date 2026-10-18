# 04 - Module-by-Module Implementation Guide for Coding Agents

> **Status note (2026-10-18):** design blueprints, not a status report — see
> `../engineering/IMPLEMENTATION-STATUS.md` and `../engineering/ROADMAP-DECISIONS.md`. Known
> deltas as of this date: the cartridge extractor is stdlib `ast` (not tree-sitter) and the
> default Tier 0 is 600 tokens at the 4,000 base (ADR-0001; the "1,200" here predates it);
> Shadow Cortex probes (Module B) are pending; Module D's `EPOCH_FOLD`/`COW_FORK` ship as
> packet-level `kernel/mmu/` and are not yet wired into the sidecar; Stage 8's
> `total_tokens <= hard_total_tokens - reserve` assertion is not enforced by the packer.

This document provides exact algorithmic blueprints for the four core components of Context Fabric v1.0. Coding agents must follow these algorithms without omitting invariants.

---

## Module A: The .cfx Cartridge Compiler (kernel/cartridge/)
**Objective:** Build the Tier 0 Project Hologram in under 500ms using zero LLM tokens.

### Algorithm:
1. **Walk Workspace:** Scan all source files, database schemas, and OpenAPI/JSON schemas in the project scope (respecting `.gitignore`).
2. **AST Extraction (ast_extractor.py):**
   - For each file `f`, parse with `tree-sitter`.
   - Extract **Definitions (symbols_exported)**: classes, interfaces, function signatures (with parameter and return types, stripping function bodies), and global constants.
   - Extract **References (symbols_imported)**: all called function names, imported modules, and referenced type identifiers.
3. **Bipartite Symbol Graph and PageRank (pagerank_map.py):**
   - Construct a directed graph `G = (V, E)` where nodes `V` are files/modules and an edge `e = (u, v)` exists with weight equal to the number of symbols defined in `v` that are referenced in `u`.
   - Run PageRank (`alpha = 0.85`) over `G` to rank every file and interface by global architectural centrality.
4. **Hologram Serialization (serializer.py):**
   - Pack the top-ranked module signatures, entry points, and architectural constraints into a deterministic Markdown tree until `tier0_cartridge` token budget (default 1,200 tokens) is reached.
   - Pad or align the rendered string to a 256-token boundary for `vLLM`/`LMCache` PagedAttention compatibility, compute `sha256`, and store in `project_cartridges`.

---

## Module B: The Shadow Cortex (router/shadow_cortex.py)
**Objective:** Execute deterministic pre-execution probes in under 15ms before compiling the packet so the agent does not waste tool calls searching for obvious context.

### Deterministic Probe Rules:
When `TaskInterpreter` processes an inbound event[cite: 22, 23]:
1. **Stack Trace / Error Log Probe:**
   - If the event or task objective contains a file path + line number pattern (e.g., `File "auth.py", line 142` or `PaymentService.ts:412:18`), the Shadow Cortex immediately parses the file via `tree-sitter`, extracts the exact enclosing function/method AST node (+/- 15 lines of context), and creates a `ContextBlock(kind="shadow_probe", memory_tier=3, creator_type="deterministic_probe", fidelity="extract")`.
2. **Symbol Impact Probe:**
   - If the task objective names specific identifiers (e.g., `refreshOAuthToken`), query `context_blocks` where `'refreshOAuthToken' = ANY(symbols_exported)` or `ANY(symbols_imported)` and attach the 1-hop caller/callee signature slice.
3. **Recent Mutation Probe:**
   - If running in a Git workspace, run `git log -n 3 --oneline` on any target file identified in Probes 1-2 and attach the recent commit summary as a 50-token `fact` block.

---

## Module C: The 9-Stage Deterministic Router and 4-Tier Packer (router/)
**Objective:** Compile a bounded, policy-checked, KV-aligned `ContextPacket`[cite: 24, 27].

### Step-by-Step Pipeline (compiler.py):
- **Stage 0 (Interpret)[cite: 27]:** Load `AgentManifest` for `recipient.agent_id`[cite: 23, 25]. Resolve `hard_total_tokens` and section budgets (`Tier 0: 15%`, `Tier 1: 25%`, `Tier 2: 5%`, `Tier 3: 45%`, `Reserve: 10%`). Pin `watermark_event_seq`.
- **Stage 1 (Recall)[cite: 27]:** Execute a single async PostgreSQL query combining:
  - Top-50 lexical matches (`search_tsv @@ plainto_tsquery(...)`).
  - Top-50 vector matches (`embedding <=> query_vec`).
  - All active `decision` and `negative_knowledge` blocks in the `workflow_id`.
  - All `shadow_probe` blocks generated for the current task.
- **Stage 2 (Hard Policy Filter - Mandatory Before Scoring)[cite: 20, 27, 32, 38]:**
  - Discard any block where `tenant_id` or `project_id` mismatches[cite: 32].
  - Discard any block where `recipient.role = ANY(denied_roles)` (e.g., when `recipient.role == 'auditor'`, block all `kind == 'observation'` created by `'implementer'`)[cite: 30].
  - Discard any block where `valid_to IS NOT NULL AND valid_to <= NOW()` or `lifecycle_status != 'routable'`[cite: 26, 27, 32].
  - Record every excluded block ID and exclusion reason code in `route_trace_json`[cite: 9, 11, 28].
- **Stage 3 (Score)[cite: 27]:** Compute deterministic utility `U(i)` using the v1.0 Cache-Aware and Counterfactual Pruning formula:
  `U(i) = w_r*Relevance + w_role*RoleFit + w_s*StageFit + w_f*Freshness + w_a*Authority + w_d*Dependency + w_c*Conflict + w_p*PruningValue - w_t*EffectiveTokenCost - w_risk*DisclosureRisk`
  where `EffectiveTokenCost = (tokens(i) / TierBudget) * (1 - cache_discount * is_cached(i))`.
- **Stage 4 (Dependency, Contradiction, and Negative Knowledge Closure)[cite: 27]:**
  - Run a recursive CTE on `block_relations` for all high-scoring candidates:
    - Pull in any required `depends_on` parent blocks[cite: 26, 27].
    - If a selected block has an incoming `supersedes` edge from a newer valid block, swap in the newer block and record a supersession trace[cite: 7, 26, 33].
    - If a selected block has an unresolved `contradicts` edge, force-include both blocks and attach a conflict alert banner[cite: 4, 27, 30, 33].
    - Force-include any `negative_knowledge` block whose `symbols_exported` or task scope intersects the selected candidate set.
- **Stage 5 and 6 (4-Tier Budget Allocation and MMR Packing)[cite: 27, 28]:**
  - **Tier 0:** Mount the project's latest `.cfx` Cartridge.
  - **Tier 1:** Pack settled `decision` and `negative_knowledge` blocks in strict chronological `event_seq` order (preserving prefix cache stability across turns).
  - **Tier 3:** Pack `instruction` block first, then `shadow_probe` blocks, then greedily select remaining `fact`/`artifact` blocks by Marginal Utility per Token with Maximal Marginal Relevance (`lambda_MMR = 0.7`) to prevent redundant chunks[cite: 27, 28].
  - **Tier 2 (Overflow -> Swap Handle):** Any candidate block that scored above the relevance threshold but exceeded the Tier 3 token ceiling—or any raw tool output evicted during prior epochs—is converted into a 1-line `SwapHandle` (`uri`, `summary`, `tokens_raw`) and placed in Tier 2[cite: 28].
- **Stage 7 (U-Curve Attention Placement)[cite: 27]:**
  - At the very bottom of Tier 3, append the **Focus and Prohibition Anchor** (a 100-token summary of the exact output schema required and a bulleted list of the active `negative_knowledge` dead ends not to repeat).
- **Stage 8 (Verify, Hash, and Sign)[cite: 22, 27]:**
  - Recount exact tokens via `tiktoken`[cite: 27]. Assert `total_tokens <= hard_total_tokens - reserve`[cite: 24, 33].
  - Compute RFC 8785 canonical JSON `sha256` hash, sign with `Ed25519`, persist to `compiled_packets`, and emit the packet[cite: 22, 25, 32].

---

## Module D: Intra-Turn Epoch Folder and CoW Forker (kernel/mmu/)

### 1. EPOCH_FOLD Algorithm (epoch_folder.py):
Triggered inside `FabricAdapter` when an agent completes step `k` (default `k = 3`) of its intra-turn tool loop OR when Tier 3 tokens exceed `fold_threshold_tokens`:
1. Take all tool calls and tool outputs from steps `1..k` in Tier 3.
2. Run deterministic extractors:
   - If a tool call failed (non-zero exit code, exception, test failure), create a `negative_knowledge` block recording the command/input and error signature.
   - If a tool call succeeded (e.g., file read, search, test pass), extract the key structured result into a compact `fact` block (max 120 tokens) and store the full raw tool output in `canonical_events` + `context_blocks(fidelity='raw')`[cite: 23, 26].
3. Create a `SwapHandle` (`ctx://obs/...`) in Tier 2 pointing to the raw tool output[cite: 26, 28].
4. Append the newly minted `fact` and `negative_knowledge` blocks to the end of **Tier 1**, clear the raw tool outputs from **Tier 3**, increment `epoch_number`, and return the folded `ContextPacket`.

### 2. COW_FORK Algorithm (cow_forker.py):
Triggered when Parent Agent `P` spawns Sub-Agent `C`[cite: 29]:
1. Read Parent Packet `P`'s `tier0_cartridge_ref` and `tier1_ledger_blocks`.
2. Initialize Child Packet `C` with the exact same `tier0_cartridge_ref` and `tier1_ledger_blocks` (filtered only if Child Agent `C` has stricter RBAC `denied_roles` than Parent `P`, e.g., when spawning an Auditor)[cite: 30, 32].
3. Allocate a clean, empty Tier 3 `working_register` for Child `C` populated only with Child `C`'s specific sub-task objective, output contract, and Shadow Cortex probes[cite: 24, 29].
4. On Child `C`'s completion, validate Child `C`'s `return_contract`[cite: 24, 29], append its verified output artifact/decisions and any new `negative_knowledge` blocks to Tier 1, and garbage-collect 100% of Child `C`'s Tier 3 scratchpad.