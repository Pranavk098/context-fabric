# Documentation Index

Everything that matters is here. "Main" documents are marked ⭐.

## product/
- ⭐ `Prod-Intro.md` — product brief, problem, paradigm, market, business model.

## design/ (the design of record)
- ⭐ `Arch-Spec.md` — normative architecture: 10 invariants, 4 planes, 4 tiers, router, scoring.
- `Db-Storage-Spec.md` — PostgreSQL schema (events, blocks, relations, packets, receipts).
- `Framework-Adapter-Spec.md` — sidecar REST API + `FabricAdapter` interface.
- `Module-Implementation.md` — algorithmic blueprints (cartridge, shadow cortex, router, MMU).
- `Repo-Tech-Stack.md` — language/libraries and monorepo layout.
- `Build-Plan-Validation-Gate.md` — phased plan and verification gates.

## research/
- `Approach-Comp-Research.md` — research foundation and competitive analysis.
- `COMPETITIVE-LANDSCAPE.md` — 2026 competitor map: what they do better, where we win.
- `SOTA-AND-THREATS.md` — current SOTA, novelty collisions, and risks to our claims.
- `REFERENCES.md` — sourced-claims ledger (verified vs `[HYPOTHESIS]`).

## engineering/
- ⭐ `ENGINEERING-LOG.md` — Stage-B findings + audit-fix results (what we built and measured).
- ⭐ `AUDIT.md` — shortfall ledger + classification (every gap: artifact/design/limitation).
- `CHANGES-FROM-PROPOSAL.md` — what changed from the original proposal and why.
- `RUNTIME.md` — how to run the vLLM / LMCache experiments locally.
- `VERIFICATION-REPORT.md` — claims vs code/results (what holds, what's overstated).
- `PROTOCOL-REVIEW.md` — deterministic-core (protocol/) audit.
- `KERNEL-REVIEW.md` — Stage C kernel code review.

## decisions/
- `ADR-0001` packet size & tier budgets
- `ADR-0002` Tier-1 watermark / cross-agent sharing semantics
- `ADR-0003` determinism model
- `ADR-0004` beachhead, scope, timeline
- `ADR-0005` CPP v1.0 frozen packet schema

---
**Consolidation note:** the standalone `STAGE-B-FINDINGS`, `AUDIT-FIXES-RESULTS`,
`SHORTFALL-AUDIT`, and `POST-B5-AUDIT` documents were merged into
`ENGINEERING-LOG.md` and `AUDIT.md` to keep the docs set lean.
