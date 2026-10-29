# Documentation Index

## Current: the Claude Code plugin (alpha)

- [`INSTALL-ALPHA.md`](INSTALL-ALPHA.md) — install, what is stored on disk, uninstall.
- [`TESTING-GUIDE.md`](TESTING-GUIDE.md) — what test users do, in order, and what to send back.
- [`decisions/`](decisions/) — architecture decisions (ADR-0006 hook transport, ADR-0007 fold, ADR-0008 brief and carry).
- [`specs/context-packet-v0.md`](specs/context-packet-v0.md) — DRAFT v0: the packet `/checkpoint` saves and restores, field by field.
- [`specs/IMPLEMENTERS.md`](specs/IMPLEMENTERS.md) — DRAFT v0 implementer notes: writer, renderer, reader, what not to claim, conformance checklist, known gaps; plus the [schema](specs/context-packet-v0.schema.json) and [test vectors](specs/vectors/).
- [`engineering/NUMBERS.md`](engineering/NUMBERS.md) — every published number, its baseline and its evidence level.
- [`engineering/HOOK-SPIKE-2026-10-21.md`](engineering/HOOK-SPIKE-2026-10-21.md) — what Claude Code hooks can and cannot do.
- [`research/HOOKS-OPENCODE-CODEX.md`](research/HOOKS-OPENCODE-CODEX.md) — hook capabilities of OpenCode and Codex.
- Code: [`cfxd/`](../cfxd/) (daemon, launcher, handlers, audit, replay), [`hooks/`](../hooks/), [`.claude-plugin/`](../.claude-plugin/).

## First-generation research: context compiler and sidecar

Everything below documents the earlier work (deterministic context compiler, OpenAI-compatible sidecar, pre-registered
results on local models). The plugin does not use this code. "Main" documents are marked ⭐.

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
- `B7-FOLD-IN.md` — original three-report promise ledger, contradicted claims, unfolded research, and external RFC/LMCache status.
- `RESEARCH-ROUND-C-EXTERNAL.md` — round-C external research.
- `SKIMMER-RESEARCH.md` — skimmer literature + approach.
- `SUBAGENT-HANDOFF-LANDSCAPE.md` — sub-agent hand-off prior art.

## engineering/
- ⭐ `IMPLEMENTATION-STATUS.md` — spec-vs-code map: what is built, partial, pending.
- ⭐ `ROADMAP-DECISIONS.md` — deliberate deferrals with triggers.
- ⭐ `ENGINEERING-LOG.md` — Stage-B findings + audit-fix results (what we built and measured).
- ⭐ `AUDIT.md` — shortfall ledger + classification (every gap: artifact/design/limitation).
- ⭐ `REPRODUCE.md` — the shortest path for a non-author to re-derive every number (`cfx repro`).
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
- `TRACK-A-REVIEW.md` — audit of the cfx CLI / writable memory / store relations (4 S2 + 9 S3 fixed, 0 S1).
- `DEMO.md` — `cfx demo`: fleet + negative-knowledge receipts, method, measured results.
- `DEMO-REVIEW.md` — independent audit of the demo fairness/numbers (0 blockers).
- `RECALL-BENCH.md` — BM25/hybrid recall benchmark + construction.
- `PACKING-QUALITY.md` — MMR + U-curve evidence (null probe stated).
- `MCP.md` — MCP stdio server reference (`cfx mcp`).
- `PERFORMANCE.md` — compile/render numbers + budget policy.
- `AGENT-HINTS.md` — RFC #52113-shaped hint producer (trace-only).
- `GAP-WAVE-REVIEW.md` — code audit of the gap wave (2 S1 + 3 S2 fixed, 0 open S1).
- `GAP-WAVE-MEASUREMENTS.md` — measurement audit (every number re-derived).
- `MMU-FOLD-FORK.md` — packet-level EPOCH_FOLD / COW_FORK semantics.
- `TRACK-CD-REVIEW.md` — code audit of Tracks C+D (3 S1 + 2 S2 fixed, verifiers hardened).
- `PILOT.md` + `PILOT-PREREGISTRATION.md` — original real-repo pilot (35.87×, hand-written blocks) + frozen criteria.
- `PILOT-MECH-PREREGISTRATION.md` + `PILOT-MECH-RESULT.md` — pilot re-run on mechanical memory (13.62× billed; uncached tokens favor the cached raw prompt).
- `B4-PREREGISTRATION-v2.md` + `B4-RESULT.md` — divergent-role experiment vs prefix caching: KILL.
- `ENGINE-MATRIX.md` — vLLM vs Ollama portability results.
- `ADAPTERS.md` — cloud adapters: OpenAI/Anthropic cache mapping.
- `SHADOW-CORTEX.md` — probe generation/evaluation v0.
- `NEGATIVE-SCORER.md` — scorer v1/v2/v3 + fresh rerun results.
- `COSTS.md` — cost model + provider presets.
- `NUMBERS.md` — canonical numbers registry (CI drift guard).
- `SCRUM-REVIEW.md` / `SCRUM-MEASUREMENTS.md` — scrum audits (code + measurements).
- `PROTOCOL-REVIEW.md` — deterministic-core (protocol/) audit.
- `KERNEL-REVIEW.md` — Stage C kernel code review.
- `REPO-CLEANUP.md` — repo-hygiene audit (dead files, packaging, gitignore/CI).
- `DOCS-AUDIT.md` — docs-consistency audit (2026-10-18): every discrepancy found and fixed.
- `A4-CONCURRENCY-PREREGISTRATION.md` + `A4-CONCURRENCY-RESULT.md` — GPU concurrency & KV-pressure experiment (preregistration + result).
- `B4-PREREGISTRATION.md` (v1) + `B4-PREREGISTRATION-v2.md` — divergent-role preregistrations (v2 is the frozen run).
- `B4-V3-PREREGISTRATION.md` + `B4-V3-RESULT.md` — the "repo does not fit" experiment (MIXED / KILL under corrected scoring).
- `B4-EXPLORE.md` + `B4-V3-OBJECTIVES.md` — B4 exploratory diagnostics and the frozen objective set.
- `B8-PREREGISTRATION.md` (v1) + `B8-PREREGISTRATION-v2.md` + `B8-DEVIATIONS.md` + `B8-HARNESS.md` + `B8-TEST-DESIGN.md` — the SWE-bench pilot preregistration, harness, and recorded deviations.
- `DOGFOOD.md` — dogfood run against the agent's own repo.
- `OBS-FOLD.md` — reversible observation folding (`cfx serve --obs-fold`).
- `REPLAY-LAB.md` — replay/receipt lab.
- `RESEARCH-ROUND-C-INTERNAL.md` — round-C internal research.
- `SKIM.md` + `SKIM-MODEL.md` — question-guided hand-off skimmers (deterministic + model-based; both exploratory).

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
