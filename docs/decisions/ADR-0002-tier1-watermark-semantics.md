# ADR-0002 — Tier 1 Watermark and Cross-Agent Sharing Semantics

- **Status:** Accepted
- **Date:** 2026-10-16

## Context

Invariant 3 (strict prefix stratification) and Invariant 7 (Copy-on-Write sub-agent
spawning) assert that an agent's Tier 0 and Tier 1 are *byte-identical* across a project
cohort, and that children share the parent's immutable pages. Cross-agent KV-cache reuse
and CoW forking are only sound if "identical" is precisely defined. The original documents
assert invariants but never specify the condition that guarantees them, making the KV-reuse
claim a latent correctness bug.

## Decision

1. **Watermark.** Every packet pins `watermark_event_seq` = the max `event_seq` in
   `canonical_events` at compile time (per `Db-Storage-Spec.md` §1 rule 2).
2. **Tier 1 is a pure function of `(tenant, project, workflow, watermark)`.** Given the same
   four values, the ordered Tier 1 block list — and therefore its rendered bytes — MUST be
   identical for every recipient. Tier 1 is rendered in strict ascending `event_seq` order.
3. **Sharing holds only within one watermark.** Two agents see the same Tier 1 prefix **iff**
   they compile against the same watermark. A later watermark appends to the end of Tier 1;
   because rendering is append-only and ordered, the *longest common prefix* with an earlier
   packet is preserved (the property vLLM/prompt caches match on).
4. **CoW fork rule.** A child packet copies the parent's `cartridge_id` (Tier 0) and Tier 1
   block list verbatim **unless** the child's `denied_roles` is strictly a superset of the
   parent's, in which case RBAC-filtered blocks are removed and the child's Tier 1 diverges
   from the parent (recorded in the route trace). A diverged child does **not** claim the
   parent's Tier 1 cache prefix.
5. **Tier 0 is invariant for a `(tenant, project, repo_commit_hash)`** and is the stable
   cache prefix for the entire cohort.

## Consequences

- "Byte-identical Tier 1" is now a testable predicate (Gate C / `Build-Plan` Gate 1), not an
  assumption.
- The watermark boundary is the unit of cache-sharing. The router must *not* interleave
  event types within a watermark's Tier 1, or prefix stability breaks.
- RBAC-filtered forks are explicitly marked as cache-divergent so the receipt's
  `kv_cache_hit_ratio` is interpreted correctly.
