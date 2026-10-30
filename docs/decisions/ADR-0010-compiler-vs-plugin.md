# ADR-0010 — The plugin is the product; the compiler is internal

- **Status:** Accepted (founder, 2026-10-30)
- **Date:** 2026-10-29
- **Work package:** D1 in `docs/plans/2026-10-29-orchestration-plan.md`; item 6 in `docs/plans/2026-10-29-ten-day-plan.md`
- **Evidence:** `CONTRIBUTING.md` (repository layout), `docs/README.md`, `README.md`, `CHANGELOG.md`,
  `docs/decisions/ADR-0004-beachhead-scope-timeline.md`, `docs/decisions/ADR-0005-packet-schema-v1.md`,
  `docs/specs/context-packet-v0.md`, `evals/b8_live.py`, `docs/engineering/RELEASE-v0.2.md`

## Context

The repo holds two generations of code. `CONTRIBUTING.md` lists `cfxd/`, `hooks/` and `.claude-plugin/` as current,
and `protocol/`, `kernel/`, `sidecar/` and `adapters/` (the context compiler, the `cfx` sidecar and framework
adapters) as "first generation; not used by the plugin". `docs/README.md` says the same. A grep on 2026-10-29 found
no import of `kernel`, `protocol`, `sidecar` or `adapters` anywhere under `cfxd/` or `hooks/`.

- The compiler's experiments are mixed. Its positive results are on the serving side with local models (window fit
  and KV use: `docs/engineering/PILOT-MECH-RESULT.md`, `A4-CONCURRENCY-RESULT.md`). On answer quality, B4 v2 was
  recorded as KILL and B4 v3 as KILL under the corrected scorer, and neither skimmer beat whole-chunk BM25
  (`CHANGELOG.md`, `docs/engineering/B4-RESULT.md`, `B4-V3-RESULT.md`, `SKIM.md`, `SKIM-MODEL.md`).
- The task-success test planned for the compiled packet is B8 arm B (`evals/b8_live.py` calls
  `kernel.router.compiler.compile_packet`). Arm B never ran (`evals/reports/b8_batch_report_20261021.md`).
- `README.md` now describes Context Fabric as "a Claude Code plugin plus offline command-line tools". ADR-0004 still
  names local GPU / vLLM operators as the beachhead and `raw_llm` + `vllm`/`lmcache` as the adapter surface.
- The plugin's saved and handed-off state is Context Packet v0 (`docs/specs/context-packet-v0.md`), built in `cfxd/`
  from observed facts. It is a different format from the compiler's packet schema v1 (ADR-0005).
- The sidecar's v0.2 release candidate is on hold and waits on B8 (`docs/engineering/RELEASE-v0.2.md`, status note).

## Decision

1. **The product is the Claude Code plugin and the `cfxd` command-line tools.** Tester instructions, install and
   release work, CI gates and every public claim are about that surface.
2. **The compiler is internal.** `protocol/`, `kernel/`, `sidecar/`, `adapters/` and the `cfx` command stay in the repo
   as research code with their tests. They get no new features and no public claims. If a plugin work package needs
   something from them, it moves the code into `cfxd/` or records a new ADR first.
3. **The public packet format is Context Packet v0.** ADR-0005's schema stays an internal format of the compiler.
4. On acceptance, this ADR supersedes decisions 1 and 2 of ADR-0004 (beachhead and adapter surface). ADR-0004's other
   decisions (gate-driven schedule, single-tenant deterministic core) are unaffected.

## Consequences

- Docs already lead with the plugin (`README.md`, `docs/README.md`), so no rewrite is forced by this ADR. Two places
  still describe the old framing and are left untouched here: the `description` in `pyproject.toml` ("semantic MMU /
  context compiler") and the B8 "in progress" line in `CHANGELOG.md`.
- The wheel still packages `protocol`, `kernel`, `sidecar` and `adapters` and installs `cfx` (`pyproject.toml`).
  Whether the plugin release keeps shipping them is left to the founder (see below).
- `ci.yml` keeps running mypy and pytest over the compiler code, so it stays working at the cost of CI time.
- Parking B8 (`docs/decisions/b8-parked.md`) follows from this ADR: B8 is a test of the compiler path, not the plugin.
- The sidecar v0.2 release stays on hold; nothing schedules it.

## Open for the founder

- Keep `kernel`/`sidecar`/`cfx` in the published wheel, or ship `cfxd` only.
