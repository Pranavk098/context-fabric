# Paper evidence dump (2026-10-31)

Everything the project currently knows that could go into a paper, gathered in one place
so it can be picked up later. **Internal working file.** Nothing here is a public claim.
All results are exploratory, from one developer's machine, unless marked otherwise.
Numbers were copied from the source files listed; re-check them against
`scripts/check_docs_numbers.py` and the sources before any reuse.

## 1. Candidate framings

| Framing | Status | What it needs |
|---|---|---|
| **A. Measurement paper**: what production coding agents spend context on, what compaction keeps, what sub-agents re-discover | Closest to ready | Logs from ≥3 developers; a frozen analysis per finding |
| **B. Systems paper**: model-sized, relevance-ranked handoff packets (Brief) reduce sub-agent cost on expensive children | Not ready; no effect measured | Brief working on the right segment (§4.3), a registered run |
| **C. Methods note**: pitfalls in benchmarking agent token cost (MCP connection race, per-line usage double count, uncounted sessions) | Ready as a short note | Clean write-up of §5 |

The current best one-sentence contribution for A: *Production compaction keeps a small
fraction of the agent's working set, and sub-agent re-discovery is small overall but
concentrated in expensive child models.*

## 2. Measurement findings (exploratory, one machine)

| # | Finding | Number | n | Source |
|---|---|---|---|---|
| M1 | Compaction summary names few held files | median **21%** of files held | 28 compactions | README evidence table; `cfxd/fidelity.py`; `docs/FIDELITY-METRICS.md` |
| M2 | Compaction summary drops edited files | **460 of 614** edited files not named | 28 compactions | same |
| M3 | Compaction summary names paths never seen | **87 of 841** named paths absent from the session | 28 compactions | same |
| M4 | Fidelity v2 frozen metrics | M1 10% unsupported paths, M2 75% dropped edits, M3 1 of 4 unsupported pass claims | exploratory | `docs/FIDELITY-METRICS.md` |
| M5 | Agents rarely re-read after compaction/clear | median **0%**, p75 < 1% of dropped context re-read | 45 compactions | `cfxd refetch`; README |
| M6 | Restart-model bound on long sessions | about **55–58%** saving *if the handoff loses nothing* (bound, not a result) | offline model | README |
| M7 | Shrinking old tool results | at most about **7%** weighted cost (optimistic bound) | 852 sessions + sub-agent runs replayed | README; `cfxd/replay.py` |
| M8 | Starting prefix | median **62K** tokens before the first prompt; about 14K from the user's own setup | this machine | README; `cfxd diet` |
| M9 | Unused setup cost | 82 unused skills + 50 unused connectors ≈ **12,466** est. tokens per turn | this machine, 2026-10-30 | `cfxd week --share` output |
| M10 | Cost concentration | sessions of 40+ turns carry **95%** of cost; top 10% of sessions carry 80% | this machine | README |
| M11 | Loops and retries | **1.4%** of weighted cost | same sessions as M7 | README |
| M12 | Sub-agent re-discovery share of child cost | **6.6% (lower) – 11.6% (upper)**, pooled by dollars | 424 children, $300.80 | `docs/gate1-rediscovery.md`; `scripts/gate1_rediscovery.py` |
| M13 | Re-discovery by child model | Opus **14.4–24.8%** (62 children, $82.26); Sonnet 3.8–6.9% (275, $208.35); Haiku 0.5–0.9% (85, $10.19) | 424 | same |
| M14 | Re-discovery by parent knowledge at spawn | parent knew ≥3 files: 7.4–13.0% (341 children); 1–2 files: 0.7–1.7%; 0 files: 0% | 424 | same |
| M15 | Exploration phase | **53.3%** of child cost before first Edit/Write (32.3% among the 213 that edit); 461 of 1,837 files read in that phase were already known to the parent | 424 | same |
| M16 | Redundant child tool calls | 6.7% of child calls (re-reads 6.6%, re-runs 0.1%); median child has zero re-discovery | 424 | same |
| M17 | No child compactions or overflows | 0 of 424; 256 of 424 children ran background/async | 424 | same |
| M18 | Real delegation volume on this machine | 278 sessions, 231 with spawns, 1,517 spawn calls, 991 sub-agent transcripts | raw count 2026-10-30 | ad-hoc count (not scripted; script it before use) |
| M19 | MCP tool-list churn in headless resume | off arm re-announced deferred tools **14–27×** per long run vs ~1× when connected before the first turn | v2 transcripts | `evals/reports/v2_registered_dedup/results.md` |

**Caveats for every row:** one developer and one machine, many research-style
delegations, exact path/command matching (M12–M16 are floors), and list-price dollars
rather than bills.

## 3. Intervention results (what did *not* work)

| Intervention | Result | Status | Source |
|---|---|---|---|
| Fold `Read` output | **+101%** tokens on read-whole-file tasks (model re-read the saved copy) | off by default | README; memory 2026-10-24 |
| Fold long command output | no measurable saving (Claude Code already truncates) | off | README |
| Checkpoint (v2 registered, dedup re-analysis, all sessions counted) | checkpoint/shipped = **1.251** long (1.182–1.309), **1.374** medium, 0.997 short: *more* expensive | disproven as built | `evals/reports/v2_registered_dedup/results.md` |
| Checkpoint "0.44" signal | artifact of counting only the last session | void | same + deviation doc |
| Off vs shipped (v2) | void: MCP connection race + per-line double count | void | `docs/deviations/2026-10-30-v2-mcp-race-and-double-count.md` |
| Brief v1 (v4 pilot, 10 runs, $1.39) | fires, 2,372 B for Haiku vs 6,000 budget, no fallback; planted-fact pass 0.833 in none/v0/v1/fork alike (parent copies facts into its prompt) | unproven; Haiku segment has ~0 to save (M13) | `docs/plans/v4-pilot-notes.md` |
| Brief v1 ordering | early user facts were pushed out by later user turns (Haiku lost all 3 after 5 turns); fixed in `57f0ff7` | fixed, offline-checked | `scripts/check_brief_ordering.py` |
| Carry-back | never fired; children came back `async_launched` | broken | pilot notes |
| B8 (SWE-bench, small window, packet vs raw) | 4 of 30 runs, all naive arm, none resolved | parked; not a result | `docs/decisions/b8-parked.md` |

Negative results belong in the paper. They are part of the credibility story.

## 4. Method assets (reusable in any framing)

1. **Paired A/B harness** (`evals/plugin_ab/`): interleaved ABBA order, per-run cost cap,
   infrastructure-only exclusions, pre-registration with SHA (`prereg*.sha256`),
   deviation docs. v2 registered at commit `00c0704`.
2. **Usage counting once per `message.id`** (`cfxd/usage_dedupe.py`). Claude Code writes
   one JSONL line per content block, each repeating `usage`. Example: one step reported
   27,630 cache writes; the true value was 9,483.
3. **MCP isolation** (`--strict-mcp-config`, `mcp_churn` infra failure). Without it,
   account connectors race the first turn and re-announce ~490 tool names per resumed
   step (~30K tokens).
4. **Planted-fact quality check** (v4): facts that exist only in the parent session, with
   hidden tests. A no-handoff control scored 0/3, so the check detects a missing handoff.
5. **Frozen fidelity metrics** (`docs/FIDELITY-METRICS.md`).
6. **Gate 1 re-discovery join** (parent `Agent` tool_use → `toolUseResult.agentId` → child
   transcript) with lower/upper cost estimates.
7. **Docs-numbers checker** (`scripts/check_docs_numbers.py`): CI fails if a published
   number drifts from its source.

## 5. Methods-note material (framing C)

- v2 registered off/shipped ratios (0.545 long, 0.685 medium) were produced by (a) the
  MCP connection race and (b) the per-line usage double count. After dedup, off-based
  ratios were still confounded (0.624 long), and the registered results were voided.
- Checkpoint looked like 0.24–0.44 of baseline because the runner kept only the final
  session of a multi-session run. Counting every session gave 1.25–1.37×.
- Lesson list: count per message, isolate MCP, count every session a run opens, check
  that delegation actually happened (`subagent_sessions: 0` in v2), and validate quality
  checks with a no-information control.

## 6. Prior art and positioning (from `docs/research/2026-10-30-handoff-prior-art.md`)

- Gap: nothing found that builds a relevance-ranked, model-sized, hash-verified brief from
  the parent's working set. Claude Code leaves it to the parent's prompt; Codex, OpenAI
  Agents SDK, Mastra and Copilot Studio default to full history.
- Closest: *Handoff Debt* (arXiv 2606.02875), on interrupted-task takeover: context-bearing
  handoffs cut prompt tokens 42–63%, with smaller and model-dependent solve-rate gains.
  *DeLM* (2606.10662): shared blackboard. Aider repo map: ranking precedent.
- Unverified in that report: Cursor, Cline, Devin, OpenHands, LangGraph, Letta,
  OpenCode. Scoop-risk estimates are internal only.
- Older landscape files: `docs/research/SUBAGENT-HANDOFF-LANDSCAPE.md`,
  `COMPETITIVE-LANDSCAPE.md`, `SOTA-AND-THREATS.md`, `REFERENCES.md`.

## 7. What a submittable paper still lacks

1. Logs from ≥3 developers (tester `week --share` and a counts-only Gate 1 export).
2. Every number in §2 produced by a committed script (M18 is not scripted yet).
3. For framing B: an intervention with a measured effect on the segment where M13/M14
   say the waste is (expensive children, parent knew ≥3 files).
4. One baseline comparison against an existing approach (full-history fork or a
   competitor's default).
5. Threats-to-validity section: one machine, list prices, exact matching, research-style
   delegation mix.

## 8. Source index

- Evidence and numbers: `README.md` (Evidence so far), `docs/engineering/NUMBERS.md`,
  `evals/reports/plugin_alpha_20261024/exploratory_results.json`
- Fidelity: `docs/FIDELITY-METRICS.md`, `cfxd/fidelity.py`
- Gate 1: `docs/gate1-rediscovery.md`, `scripts/gate1_rediscovery.py`
- Benchmarks: `evals/plugin_ab/PROTOCOL*.md`, `evals/reports/v2_registered_dedup/`,
  `docs/deviations/`, `docs/plans/v4-pilot-notes.md`, `docs/plans/v4-item1-design-note.md`
- Decisions: `docs/decisions/ADR-0010-compiler-vs-plugin.md`, `ADR-0011` (Brief v1),
  `docs/decisions/b8-parked.md`
- Earlier engineering results: `docs/engineering/B4-RESULT.md`, `B4-V3-RESULT.md`,
  `A4-CONCURRENCY-RESULT.md`, `PILOT-MECH-RESULT.md`, `B8-DEVIATIONS.md`
- Research: `docs/research/` (all files)
