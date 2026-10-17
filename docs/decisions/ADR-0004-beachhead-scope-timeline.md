# ADR-0004 — Beachhead, v1.0 Scope, and Timeline

- **Status:** Accepted
- **Date:** 2026-10-16
- **Supersedes:** market/scope/schedule sections of `Prod-Intro.md`, `Build-Plan-Validation-Gate.md`.

## Context

The proposal addressed three markets simultaneously, shipped eight adapters, and scheduled
the whole of v1.0 in "Days 1–32". For a product intended to be built without cutting
corners, a single beachhead and an honest, gate-driven schedule are prerequisites.

## Decision

1. **Beachhead = local GPU / vLLM operators.** Rationale: sharpest, most measurable pain
   (VRAM OOM, KV thrashing), runs on hardware we control, and has a concrete ally in
   LMCache. Cloud-API and enterprise/RBAC become expansions, not v1.0.
2. **v1.0 adapter surface = `raw_llm` + `vllm`/`lmcache` only.**
   `langgraph`, `crewai`, `autogen`, `mcp_server`, `a2a_bridge` are deferred to post-v1.0.
3. **v1.0 is single-tenant, deterministic-core.** No learned rerankers/compressors, no
   multi-tenant hosting, no RBAC policy packs (the RBAC *engine* stays; enterprise policy
   *packs* are commercial, post-v1.0).
4. **Schedule is gate-driven, not calendar-driven.** Phases as in `CHANGES-FROM-PROPOSAL.md`
   §4–5. No phase starts until the previous phase's gate passes. The "Days 1–32" estimate is
   retracted.
5. **The falsification spike (Stage B) precedes kernel construction.**

## Consequences

- All benchmark suites target the local-serving workflow; a "cloud cost" suite is deferred.
- Framework-agnosticism is a v2 claim; v1.0 proves the mechanism on one runtime family.
- Progress is measured by gates passed and by the Gate B/C numbers, not by feature count.
