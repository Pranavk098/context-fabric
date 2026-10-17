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
- `PLUG-PRIOR-ART.md` — integration plays: what worked, what died, and the plug shape.
- `PROVENANCE-PRIOR-ART.md` — receipts/signing prior art + the minimum credible seal.
- `SHARED-MEMORY-PRIOR-ART.md` — cross-agent sharing prior art; the unclaimed contract.
- `PATH-VIABILITY.md` — brutal market/post-mortem check + falsification tests per step.
- `REFERENCES.md` — sourced-claims ledger (verified vs `[HYPOTHESIS]`).

## engineering/
- ⭐ `ENGINEERING-LOG.md` — Stage-B findings + audit-fix results (what we built and measured).
- ⭐ `AUDIT.md` — shortfall ledger + classification (every gap: artifact/design/limitation).
- `CHANGES-FROM-PROPOSAL.md` — what changed from the original proposal and why.
- `RUNTIME.md` — how to run the vLLM / LMCache experiments locally.
- `VERIFICATION-REPORT.md` — claims vs code/results (what holds, what's overstated).
- `RERUN-REVIEW.md` — audit of the 2026-10-17 cold-boot/KV re-run (fairness, scorer, numbers).
- `RESEARCH-AUDIT.md` — citation verification of the four research docs (5 corrections, 0 fabrications).
- `SIDECAR.md` — the plug: 10-minute setup, A/B recipe, troubleshooting.
- `SIDECAR-REVIEW.md` — adversarial audit of the sidecar (5 S2 fixed, 0 S1).
- `SEAL.md` — signed epoch receipts: claims, usage, honest limits.
- `SHARED-COPY.md` — memory-state CoW consistency contract (fork/fold).
- `STORE.md` — SQLite store v0 + Postgres migration path.
- `EPOCHS-STORE-REVIEW.md` — audit of the CoW + store code (4 S2 fixed, 0 S1).
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
