# ADR 0002 — ContextDB is out of scope until after the v3 benchmark results

- **Status:** Proposed (founder confirms in the PR)
- **Date:** 2026-10-29
- **Work package:** D1 in `docs/plans/2026-10-29-orchestration-plan.md`; item 6 in `docs/plans/2026-10-29-ten-day-plan.md`
- **Evidence:** `docs/plans/2026-10-29-ten-day-plan.md` (§2), `docs/design/Db-Storage-Spec.md`,
  `docs/engineering/ROADMAP-DECISIONS.md`, `docs/decisions/ADR-0008-brief-and-carry-from-observed-facts.md`,
  `docs/specs/context-packet-v0.md`, `evals/plugin_ab/PROTOCOL_V3_EXPLORATORY.md`

## Context

"ContextDB" appears in the repo only in the two 2026-10-29 plans. No document defines it. This ADR takes it to mean a
persistent, queryable store of context shared across sessions or agents: the shape of the design-of-record storage
spec (`docs/design/Db-Storage-Spec.md`: append-only event log, derived block store, PostgreSQL), whose shipped subset
is the SQLite store in `kernel/store/`. If the founder meant something else, this ADR needs rewriting, not just
confirming.

- The plugin does not need a database today. Session facts live in memory, carry writes one bounded JSON snapshot,
  receipts are JSONL files (ADR-0008), and `/checkpoint` and cross-agent handoff read and write Context Packet v0 files
  (`docs/specs/context-packet-v0.md`, `cfxd/handoff.py`).
- PostgreSQL was already deferred, with the trigger "team/hosted deployment, or cross-store joins required"
  (`docs/engineering/ROADMAP-DECISIONS.md`, row 4). Neither has happened.
- The ten-day plan lists ContextDB under "Explicitly not before testers" (§2).
- No mechanism that acts on context has a measured effect yet (`README.md`, status). The registered v3 plugin
  benchmark (WP A6, run in A7, analysed in P2) is the next result that could show whether the packet path is worth
  building on. The adversarial chains in `evals/plugin_ab/PROTOCOL_V3_EXPLORATORY.md` are exploratory, not that result.

## Decision

1. **No ContextDB work until the registered v3 results are analysed (WP P2).** No schema, no service, no new store
   module, and no public mention of one.
2. The plugin keeps its file-based state: in-memory session facts, the carry snapshot, JSONL receipts, and Context
   Packet v0 files.
3. `kernel/store/` (SQLite) stays as it is under ADR 0001 (internal, no new features).
4. After P2, ContextDB is reconsidered only with a written problem it solves that files cannot, and a new ADR.

## Consequences

- The sprint keeps its file sets small; no WP in the orchestration plan needs a database.
- Cross-session or cross-agent queries (for example, "which packets mention this file") are not possible except by
  reading packet files. That is accepted for the alpha.
- If v3 shows no effect for checkpoint, a shared store has nothing to serve and the question may close for good. If
  it shows one, the reconsideration starts from the v3 data, not from `Db-Storage-Spec.md`.

## Open for the founder

- Confirm what "ContextDB" means. This ADR assumes the persistent store above.
- Confirm that "after v3" means after the P2 analysis of the registered run, not after the exploratory adversarial set.
