# Brief-first plan (2026-10-30 → 2026-11-08)

Supersedes the priorities in `2026-10-29-ten-day-plan.md` and the waves after Wave 1 in
`2026-10-29-orchestration-plan.md`. Those files stay as history.

## Why the plan changed

The project's founding thesis: **when a big model delegates to a small one, the handoff
is where context is wasted.** Under-briefed sub-agents re-discover what the parent already
knew. Over-briefed ones overflow, compact and lose work. Either way, cost goes up and
quality goes down. Context Fabric exists to fix that handoff.

Since then the work drifted into measurement. The evidence:
- The v2 registered benchmark recorded `subagent_sessions: 0` on every step. It never
  exercised a handoff.
- Brief v0 (`cfxd/brief.py`, 248 lines) is on by default but has **never fired in any
  measured run**. It is a ≤2 KB list of pointers (paths, line counts, edit flags, folded
  outputs, recent commands). It does not know which model it briefs, ranks nothing by task
  relevance, and carries no content, decisions or constraints.
- B8, the only experiment aimed at the thesis, is parked (`docs/decisions/b8-parked.md`):
  4 of 30 runs, all in the naive arm.

The measurement work is not wasted. It becomes the instrument. Usage counting, MCP
isolation, the harness, fidelity metrics and packet v0 are exactly what a handoff
experiment needs.

## Where we are (verified 2026-10-30)

Wave 1 is **done and green on main** (CI `plugin`, `macos`, `ci` all pass at `ce67314`):

| WP | Result |
|---|---|
| A1 MCP isolation | `--strict-mcp-config` for all arms; `mcp_churn` infra failure; `tests/test_mcp_isolation.py`. Open: `ENABLE_CLAUDEAI_MCP_SERVERS` unverified |
| A2 usage dedup | `cfxd/usage_dedupe.py`, wired into audit/diet/replay/routing/waste and `evals/plugin_ab/usage.py`; `tests/test_usage_dedupe.py`. Open: stored v2 records are still per-line |
| C1 macOS CI | `.github/workflows/macos.yml` green (install, doctor, fixture audit) |
| D1 decisions | ADR-0010 (plugin is the product, compiler internal); B8 parked |

## The goal for day 10

1. **Brief v1 shipped.** A model-aware, task-relevant handoff packet that a parent passes
   to sub-agents automatically, with an optional return path (child → parent summary).
2. **A registered delegation benchmark run** that tests the thesis directly: big parent,
   small children, Brief v1 vs no brief vs full-context fork, with quality checks that
   can only pass if the handoff carried information.
3. **A paper skeleton** whose contribution sentence is the delegation result, with
   fidelity/refetch as the motivation section.
4. **A tester build** whose headline feature is Brief, plus `cfxd brief` receipts showing
   what each child received and what it re-read anyway.

One-sentence pitch (no number until the benchmark gives one): *Big models brief small
models badly. Context Fabric fixes the handoff.*

## Brief v1: what it is

| Part | v0 today | v1 target |
|---|---|---|
| Trigger | PreToolUse on `Agent` | same, plus an explicit `/brief` command and a library call for other frameworks |
| Model awareness | none | budget from the child model (`subagent_type` / `model` field → window and tier). Small models get a tighter, more explicit packet |
| Selection | most recent N files | ranked by task relevance: paths/symbols named in the child's prompt, parent edits, import neighbours, recency |
| Content | pointers only | pointers **plus** short, verified excerpts: the line ranges the parent read or edited for top-ranked files, within budget |
| Intent | none | parent goal, decisions, constraints, "already tried". Taken from the **parent's own messages and the delegation prompt**, never from tool output (keeps v0's injection rule; ADR needed) |
| Format | ad-hoc tag | emits **packet v0** (`cfxd/packet.py`), so the same brief can go to Codex/OpenCode via handoff |
| Return path | none | optional `carry-back`: when the child finishes, a capped summary of files touched, result and open issues is appended for the parent instead of the raw transcript |
| Receipts | `briefs.jsonl` decision | per brief: bytes, items, target model, plus the child's re-reads of briefed files, tokens and compactions (joined after the fact) |
| Safety | 2 KB cap, sanitised | per-model cap (default Haiku ≈ 6 KB, Sonnet ≈ 12 KB; tuned by pilot), same sanitising, excerpts verified against current file hashes |

## Delegation benchmark (v4, registered)

- **Fixture:** a real mid-size repo vendored at a pinned commit (≥150 files, a test
  suite), plus 6–8 tasks that each require reading across several modules. The parent
  prompt asks it to plan and delegate implementation sub-tasks to Haiku sub-agents.
- **Planted-fact quality check** (replaces the broken recall test): early in the parent
  session, the prompt states facts the child needs and that exist nowhere in the repo (a
  required constant name, a constraint like "do not touch module X", a decision). Hidden
  tests check the child obeyed them. **A run where the child never received them should
  fail those checks.** A no-handoff control validates this.
- **Arms** (all with MCP isolation and dedup counting):
  1. `none`: plugin loaded, Brief off. The parent writes whatever prompt it writes.
  2. `brief_v0`: today's pointer list.
  3. `brief_v1`: the new packet.
  4. `fork`: a sub-agent that inherits the full parent conversation (over-briefing
     baseline).
  5. `control`: the child gets only the task line, no planted facts (validates the
     quality check).
- **Models:** parent Sonnet (cost-controlled stand-in for "big"; one Opus slice if the
  budget allows), children Haiku.
- **Primary outcome:** total cost (parent + children, dedup counted) at equal or better
  pass rate on hidden tests including planted-fact tests. Comparison: `brief_v1` vs
  `none`.
- **Secondary outcomes:** child re-reads of files the parent already read; child
  compactions/overflows; planted-fact pass rate; `brief_v1` vs `fork`; `brief_v1` vs
  `brief_v0`.
- **Process:** pilot (unregistered, ≤$10) to size budgets and catch harness bugs →
  freeze protocol with SHA → run overnight → analyze as registered. Any confound gets a
  deviation entry, as with v2.

## What is dropped or demoted

- Checkpoint-vs-shipped v3: **demoted** to a secondary run if there is spare budget after
  v4. A3/A4/A5 from the old plan shrink to what v4 needs (A5's off≈shipped probe becomes
  part of the v4 pilot).
- Report card, extra waste rules, public bench docs: **after testers**.
- Fidelity command: kept small. It's the paper's motivation and a tester hook, one WP.
- B8: stays parked. v4 tests the same question on the plugin path that ADR-0010 makes the
  product.

## Day map

| Day | Build | Gate |
|---|---|---|
| 1 ✅ | Wave 1 (A1, A2, C1, D1) | CI green, done |
| 2 | Brief v1 design ADR + intent-source rule; v4 fixture selection; receipts joiner | founder signs ADR |
| 3 | Brief v1 core: model budgets, relevance ranking, excerpts, packet v0 output | unit tests; byte caps hold per model |
| 4 | Brief v1 intent + carry-back; v4 harness arms (`fork`, `control`, planted facts) | fake-claude end-to-end passes for every arm |
| 5 | v4 pilot (real, small); fix what it finds; tune budgets | control fails planted facts; no `mcp_churn`; children actually spawn |
| 6 | Freeze v4 protocol (SHA) → overnight run. In parallel: `cfxd brief` receipts view, cross-agent brief demo | run started |
| 7 | Tester build: install script + release workflow, TESTING-GUIDE around Brief; fidelity command | clean install < 5 min on Windows + macOS CI |
| 8 | v4 analysis as registered; deviations if any | analysis doc |
| 9 | Paper skeleton: thesis, method, v4 results, fidelity/refetch motivation, threats | docs-numbers covers `paper/` |
| 10 | README rewrite around Brief (proven claims only); tester handoff; post draft (unpublished until results hold) | — |

## Risks

- **Children don't spawn reliably** with scripted prompts. Mitigation: the pilot checks
  the spawn rate per arm; the prompt names the sub-agent type and model explicitly.
- **Intent extraction leaks tool output into packets.** Mitigation: ADR + tests that seed
  injection strings into tool results and assert they never reach the packet.
- **Cost.** Mitigation: Sonnet parent, Haiku children, per-run cap, pilot first.
- **A result in our disfavour.** It gets published the same way. A null on `brief_v1`
  vs `none` with a win over `fork` is still a paper.
