# Changes From Proposal — Decision & Delta Log

**Status:** Working document — for your deliberation
**Date:** 2026-10-16
**Author:** engineering (agent-assisted)
**Scope:** Compares the eight original documents against the direction agreed in this session, and records every deviation, the reason for it, and what remains open.

---

## 0. Purpose

This document is the single place to see *what changed and why* since the original
Context Fabric proposal. It exists so we can align before deeper build work, and it
doubles as the seed of the project's decision log.

Read order if you want the short version: **§2 (delta table) → §3 (locked decisions)
→ §7 (open questions)**.

---

## 1. Where we were (the proposal as written)

Eight documents, all dated 2026-10-16:

| File | Role |
| :--- | :--- |
| `Prod-Intro.md` | Executive/pitch brief (problem, paradigm, market, business model) |
| `Approach-Comp-Research.md` | Research foundation + competitive analysis |
| `Arch-Spec.md` | Normative architecture (invariants, 4 planes, 4 tiers, scoring) |
| `Repo-Tech-Stack.md` | Tech stack + monorepo tree |
| `Db-Storage-Spec.md` | Full PostgreSQL DDL |
| `Framework-Adapter-Spec.md` | REST API + adapter ABC |
| `Module-Implementation.md` | Algorithmic blueprints (cartridge, shadow cortex, router, MMU) |
| `Build-Plan-Validation-Gate.md` | Phased plan (Phase 0–4) + verification gates |

The **product thesis** (semantic MMU / prefix-aligned context compiler for agents) is
unchanged and remains the reason to build this. What changed is *how we sequence,
scope, and justify* the build.

---

## 2. What we changed, and why (delta table)

| # | Topic | Original (as written) | Change made | Why |
| :- | :--- | :--- | :--- | :--- |
| **C1** | Packet size | Internally contradictory: pitch says "3,000-token" window, Arch tiers (1200+2000+400+3600+800) sum to ~8,000, invariant says "≤3,500 dynamic" | Lock **one** source of truth: base `hard_total_tokens = 4,000` (configurable per manifest); tiers 15/25/5/45/10; "**~3,000 dynamic**" = Tier1+Tier2+Tier3; Tier0 = 600 static; reserve 400 | Removes a 2.5× contradiction in the most load-bearing number of the product; the reconciliation also makes both pitch numbers (`3,000` dynamic, `3,500` incl. cartridge) true simultaneously. See `docs/adr/ADR-0001`. |
| **C2** | Citations | ~38 `[cite: N]` markers, **no bibliography**; several late-2026 claims unattributed to a resolvable source | Add `REFERENCES.md`; verify anchors against live sources; any numeric claim we cannot source is retagged `[HYPOTHESIS]` (to be measured, not quoted) | A research-backed product cannot ship pitch numbers it cannot defend. Displayed counts so far: **3 verified**, **2 partially verified**, **several unverified**. See §6. |
| **C3** | Schedule | "Days 1–32" for full v1.0 (8 adapters, router, MMU, UI, 4 benchmark suites, hardening) | Removed calendar dates; plan is now **gate-driven, solo, phase-ordered, open-ended** | 32 days is not achievable for this scope by any team; keeping the number would corrupt prioritization. See `docs/adr/ADR-0004`. |
| **C4** | Adapter scope | Phase 3 ships LangGraph + CrewAI + AutoGen + MCP + A2A + LMCache + raw-LLM | **v1.0 = `raw_llm` + `vllm`/`lmcache` only.** LangGraph/CrewAI/AutoGen/MCP/A2A deferred to post-v1.0 | Focus on the beachhead. Adapter breadth was optimising for "framework-agnostic" marketing before the core mechanism is proven. |
| **C5** | Beachhead | Names three markets at once (cloud super-users, local GPU operators, enterprises) | **Locked: local GPU / vLLM operators** | Sharpest pain (VRAM OOM, KV thrashing), measurable on owned hardware, concrete symbiosis with LMCache. Enterprise/RBAC and cloud become later expansions. |
| **C6** | Determinism | "Deterministic core, learned edge" stated, but embeddings/MMR/tie-breaks unspecified | v1.0 ships **zero learned components**; determinism model specified (embedding version pinning, tie-break rules, PageRank convergence, signing latency budget) | "Cryptographically replayable" is an invariant; it is unimplementable without pinning the non-obvious sources of nondeterminism. See `docs/adr/ADR-0003`. |
| **C7** | Sequencing | Phase 0 (measure baseline) → Phase 1 (build kernel) directly | Insert **Stage B: a falsification spike** between baseline measurement and kernel build | The entire value prop rests on an **unproven** assumption: that real vLLM/LMCache grant prefix-cache reuse across turns *and across CoW forks*, and that this cuts cost/VRAM without quality loss. We prove or kill that before building the router. |
| **C8** | Engineering process | No repo, no git, no ADRs, no decision log | `git init`; `docs/adr/` with numbered ADRs; `protocol/` + `evals/` scaffolded; each phase ends in one runnable check | Real-product SDLC. |
| **C9** | Target hardware | "Local GPUs (16GB–24GB VRAM)" | This dev machine is an **RTX 5070 Laptop, 8GB VRAM, Windows** — *below* the design target | The spike must run under 8GB using small models (e.g. 0.5B–1.5B) and via WSL2/Docker (vLLM is Linux-first). If the win is not visible even on a tiny model, that is itself a critical finding. |
| **C10** | Cross-agent Tier 1 sharing | Invariant 3 & 7 assert byte-identical Tier 0/1 and CoW sharing, but the *watermark/superset semantics* that make it true are unspecified | Specify exactly when two packets are guaranteed the same Tier 1 prefix (see `docs/adr/ADR-0002`) | This is the mechanism that makes fork-sharing and cross-agent cache reuse sound. Unspecified, it is a latent correctness bug in the KV-reuse claim. |

---

## 3. Decisions locked this session

1. **Beachhead:** local GPU / vLLM operators.
2. **Approach:** risk-first spike + targeted doc fixes (not broad architecture review, not a full plunge).
3. **Team/timeline:** solo, flexible, strictly sequenced phases.
4. **v1.0 surface:** `raw_llm` + `vllm`/`lmcache` adapters only; deterministic core; single-tenant.
5. **Process:** gate-driven; every phase produces one executable, falsifiable check; the core cache hypothesis is proven before the router is built.

---

## 4. Stage A — Documentation corrections (in progress)

These are the minimum doc fixes that unblock clean implementation. No code risk.

- [x] `docs/CHANGES-FROM-PROPOSAL.md` (this file)
- [x] Reconcile token budgets across `Arch-Spec.md` §1/§3, `Prod-Intro.md` §2 (C1)
- [x] `REFERENCES.md` — resolve cites, retag unverifiable numerics (C2)
- [x] `docs/adr/ADR-0001` packet size & tier budgets
- [x] `docs/adr/ADR-0002` Tier 1 watermark / cross-agent sharing semantics
- [x] `docs/adr/ADR-0003` determinism model
- [x] `docs/adr/ADR-0004` beachhead, scope, timeline
- [x] `docs/adr/ADR-0005` CPP v1.0 frozen packet schema

**Gate A:** budgets reconcile to one number; every pitch number is sourced or tagged
`[HYPOTHESIS]`; ADRs 1–5 written.

---

## 5. Stage B — Falsification spike (next, this is the real bet)

**Hypothesis to falsify:** *On real vLLM + LMCache, prefix-aligned packets preserve KV
reuse across (a) intra-turn tool steps and (b) CoW sub-agent forks, and cut tokens/VRAM
without task-quality regression.*

First code artifacts:
- `protocol/models.py` — Pydantic v2 immutable models for `ContextPacket`, `ContextBlock`, `WorkflowEvent`.
- `protocol/canonical.py` — RFC 8785 JCS serializer + sha256.
- `evals/harness.py` — wraps vLLM calls; records input/cached/dynamic tokens, prefix-hash continuity, TTFT, prefill ms, peak VRAM.
- One synthetic workflow: single agent × 15 tool steps (tests `EPOCH_FOLD`) + parent→child `COW_FORK`.
- Two arms: **B0 full-broadcast** vs **shared-prefix packet**.

**Gate B (decision):**
- **Pass** → proceed to kernel (Stage C). 
- **Fail** → thesis does not hold on the beachhead as written; pivot (e.g. lean on LMCache `CacheBlend` non-prefix reuse, or narrow to intra-turn folding only) *before* building the router.

### Outcome (2026-10-16) — see `docs/STAGE-B-FINDINGS.md`

The spike **ran successfully** on vLLM 0.31.0 + Qwen2.5-1.5B (8 GB RTX 5070). Result:
**partially passes, INCONCLUSIVE.**

- ✅ Mechanism works; harness + vLLM `/metrics` measurement trustworthy.
- ✅ Token-volume reduction of **50%** (CPP 19.7k vs B0 39.5k prompt tokens) on a 15-step loop.
- ❌ **Uncached/prefill work was 49% *higher* for CPP** (7.8k vs 5.2k). With a cache
  discount, effective cost is a **wash** intra-agent.
- ❌ Found a **rendering-order defect**: Tier 1 (ledger) growing *before* Tier 2 (swap
  pointers) breaks CPP's own prefix cache every turn.
- ❌ The actual thesis — **cross-agent / CoW-fork** sharing — is **not yet tested**.

**Decision:** do **not** proceed to the kernel yet. Add **Stage B2** (corrected rendering
order; baseline B0-no-cache; multi-agent CoW-fork arm; billed-cost model). See
`STAGE-B-FINDINGS.md` §5.

### Stage B2 — corrected multi-agent experiment — **DONE (2026-10-16): PASS**

Ran corrected intra-agent + 8-child CoW fork (`evals/stage_b2.py`; report
`evals/reports/stage_b2_20261016_145513.json`; write-up `STAGE-B-FINDINGS.md` §7).

- **Intra-agent: still a tie** (effective cost 7,806 vs 7,753) — prefix caching neutralizes
  the naive baseline within one agent. Confirms the sale is *not* intra-agent.
- **Multi-agent fork: decisive win** (magnitudes corrected by the clean B3 sweep).
  - vs **realistic naive** (task at top): **~3.6×–12× lower effective cost** (grows with N).
  - vs **best-case naive** (byte-stable prefix): **~1.8× (medium) – 2.6× (large context) lower**.
  - 8 children share one Tier 0+1 prefix at **95%** cache hits.
  - **Crossover:** below ~10 parent steps the fixed cartridge overhead is not amortized and
    CPP is *worse* (0.82×) — the win is conditional on context size.

**Reframed value proposition (from the data):** prefix caching is a *competitor*; the
product wins on **(V1) guaranteed prefix alignment by construction**, **(V2) compression of
the shared prefix** (~2–3× here — the headline metric), and **(V3) role-differentiated,
bounded context**. Gate B2/B3 = **PASS**.

### Stage B4 (quality) + B5 (concurrency/VRAM) — **DONE (2026-10-16): PASS, with caveats**

- **B4 quality:** faithful compilation was **lossless** in a labeled extractive task — 100%
  accuracy at 6.1× fewer tokens — and accuracy tracks *retention exactly* (dropped fact =
  lost answer). **Caveat:** the task is trivial; the real quality benchmark needs the actual
  compiler (GAP-22/25).
- **B5 concurrency/VRAM:** at 16 concurrent agents CPP held **~1.4% KV cache vs B0's 28%
  (20× lower)**, delivered **8.6× throughput** (44.8 vs 5.2 req/s), and never queued. This
  **closes GAP-07** (the KV/VRAM-wall claim). Caveats: prefill-vs-KV attribution and no
  error bars (GAP-26/27).

### Where the results underperform — `docs/SHORTFALL-AUDIT.md`

27 tracked gaps (GAP-01…GAP-27) with severities and a prioritized review checklist for the
**post-B5 audit**: GAP-01 (the unproven 15× claim), GAP-12 (baseline fairness), GAP-02
(intra-agent tie), GAP-04/25 (compression reality), GAP-06 (small-context crossover/TAM),
GAP-22 (weak quality proxy), GAP-26 (attribution), GAP-27 (no error bars).

---

## 6. Claim-verification status (from live sources, 2026-10-16)

| Claim in docs | Status | Note |
| :--- | :--- | :--- |
| ReFold (Oct 2026) context folding, k-step chunked rendering, prefix-cache aware | **Verified** | arXiv:2610.07863, "ReFold: Training-Free Reversible Inter-Turn Context Folding", 6 Oct 2026. Reports up to 2.5× token reduction, ~50% KV-cache memory; best at k=3–5. |
| EFFICIENTAGENT (Sept 2026); naive masking dropped prefix-cache hit 96.5%→51%, +31% cost | **Unverified** | No source located under that name. Closest real work: "The Complexity Trap" (arXiv:2508.21433) and "Don't Break the Cache" (arXiv:2601.06007). The *specific* 96.5%→51% / +31% figures are **not confirmed** → retag `[HYPOTHESIS]`. |
| Prompt caching yields large cost/TTFT savings for agentic tasks | **Verified (mechanism)** | "Don't Break the Cache" (arXiv:2601.06007): 41–80% cost reduction, 13–31% TTFT improvement, exact-prefix-match required. |
| CacheBlend / LMCache non-prefix reuse; vLLM PagedAttention; SGLang RadixAttention | **Verified (mechanism)** | Cited in arXiv:2610.05833 and LMCache/vLLM/SGLang literature. Design depends on them — must be re-confirmed on our hardware. |
| "Multi-agent systems consume ~15× tokens of a single chat" | **Not yet sourced** | Retag `[HYPOTHESIS]` until sourced or measured by our own harness. |
| MCP spec "2026-07-28", A2A v1.0 (2026) | **Out of v1.0 scope** | Deferred (C4); revisit when adapters resume. |

> Rule going forward: a number is either **(a)** sourced to a verifiable reference, or
> **(b)** tagged `[HYPOTHESIS]` and assigned to a spike measurement. No third category.

---

## 7. Open questions for your deliberation

1. **Token budget (C1):** Do you accept `hard_total_tokens = 4,000` as the v1.0 default,
   with `3,000` = dynamic (Tier 1+2+3) and Tier 0 = 600? Or do you want the base higher
   (e.g. 8,000) and the pitch restated?
2. **8GB VRAM reality (C9):** the dev machine is below the design target. Do we (a) spike
   on small models here, (b) use a rented GPU (RunPod/Colab), or (c) pursue both?
3. **Windows vs WSL2:** vLLM is Linux-first. Accept WSL2/Docker for the spike on this
   machine, or run the spike on a Linux box?
4. **Scope cut (C4):** confirm we drop CrewAI/AutoGen/MCP/A2A from v1.0 entirely.
5. **"15× tax":** do you have the source for this figure, or do we treat it as a
   hypothesis the harness verifies?

---

## 8. What did NOT change

- The core thesis: agent context window as an L1 register; a semantic MMU compiling
  bounded, prefix-aligned, auditable packets.
- The 10 architectural invariants in `Arch-Spec.md` §1 (only the *numbers* in invariant 1
  move, per C1).
- The 4-plane reference architecture and 4-tier memory model.
- The 9-stage router design, MMU syscalls, and negative-knowledge concept.
- The open-core + enterprise commercial framing.
