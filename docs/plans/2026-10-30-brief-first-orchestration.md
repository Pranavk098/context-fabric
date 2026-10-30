# Brief-first orchestration: work packages for Orca

The how for `2026-10-30-brief-first-plan.md`. Replaces Waves 2–5 of
`2026-10-29-orchestration-plan.md` (Wave 1 there is done). Branch prefix: `s11-<wp-id>`.

## Ground rules for every agent

Paste this block at the top of every task.

```
Repo: github.com/Pranavk098/context-fabric. Work on your own branch `s11-<wp-id>` from main.
Read CLAUDE.md / AGENTS.md and docs/plans/2026-10-30-brief-first-plan.md first. Python, uv, ruff, pytest.
Before you finish: `uv run ruff check .`, `uv run pytest -q`, and `uv run python scripts/check_docs_numbers.py` must pass.
No new public claims. Do not edit README headline numbers, registered protocols (PROTOCOL*.md, *PREREGISTRATION*),
or anything under evals/reports/. Touch only the files your WP lists; if you need another, stop and say so.
Brief packets are built from session facts, the parent's own messages and the delegation prompt, never from tool output text.
Commit with a clear message; do not push or merge. End with: files changed, tests added, commands run + output, open questions.
```

- `C:\dev\cf-bench` is for benchmark runs only. Pull main there just before a run.
- The "Files" column is a lock. WPs with overlapping files never run at the same time.
- The coordinator (Opus) runs every acceptance check itself before handing a branch to the
  founder to merge. Update `docs/plans/STATUS.md` every evening.
- Before any push: `gh auth switch --user Pranavk098`.

## Agent routing

| Agent | Use for |
|---|---|
| **Opus (coordinator)** | ADRs, protocol/registration, analysis, paper, reviews, merges |
| **Sonnet** | Brief v1 implementation and harness arms (needs judgment + tests) |
| **OpenCode** | Fixture vendoring, CI/release, install script, docs, demos. Give the full brief each time (`--session` resume runs headless) |

## Dependency graph

```
Wave 2 (day 2, parallel):  B0 Brief v1 ADR (Opus)   H1 v4 fixture + tasks (OpenCode)   R1 receipts joiner (Sonnet)
Wave 3 (day 3, parallel after B0):
                           B1 budgets + relevance ranking (Sonnet)
                           B2 excerpts + packet v0 output  (Sonnet)  ← shares brief.py with B1: B1 first, B2 rebases; or split modules as listed
Wave 4 (day 4):            B3 intent + carry-back (Sonnet, after B1/B2)
                           H2 v4 harness arms + planted facts (Sonnet, after H1)
Wave 5 (day 5):            P0 v4 pilot (founder runs; Opus reads)  ← GATE
Wave 6 (day 6):            H3 freeze v4 protocol (Opus) → founder overnight run
                           U1 `cfxd brief` receipts view (Sonnet)   X1 cross-agent brief demo (OpenCode)
Wave 7 (day 7):            C2 release + install (OpenCode)   T1 tester guide (OpenCode)   F1 fidelity command (Sonnet)
Wave 8–10:                 A8 v4 analysis (Opus)   PP paper skeleton (Opus)   D2 README (Opus, last)   D3 post draft
```

**Gate P0:** v4 does not register until the pilot shows children spawn in every arm, the
`control` arm fails planted-fact tests, there are zero `mcp_churn` failures, and per-run
cost fits the cap.

---

## Wave 2

### B0 · Brief v1 design ADR
- **Agent:** Opus · **Files:** `docs/decisions/ADR-0011-brief-v1.md`
- **Decide:**
  - The packet sections (task, intent, constraints, decisions, already-tried, files with
    excerpts, folded outputs, commands).
  - Per-model budgets (initial: Haiku 6 KB, Sonnet 12 KB, Opus 20 KB, unknown 4 KB). How
    the target model is read (`model` / `subagent_type` in tool input, else default).
  - **Intent-source rule:** only the parent's assistant messages, user prompts and the
    delegation prompt; never `tool_response`. How excerpts are verified (file hash at brief
    time, read from disk, never from transcripts).
  - Carry-back shape and cap. Mapping onto packet v0 (`cfxd/packet.py`). What stays out of
    scope.
- **Accept:** founder sign-off. Every later B-WP cites it.

### H1 · v4 fixture and tasks
- **Agent:** OpenCode · **Files:** `evals/plugin_ab/fixture_v4/` (new),
  `evals/plugin_ab/tasks_v4.json` (new)
- **Do:**
  - Vendor a permissively licensed Python repo at a pinned commit: 150–400 files, a fast
    test suite, no network in tests. Record the license and commit in `fixture_v4/SOURCE.md`.
  - Write 8 tasks. Each needs edits in 2+ modules, is solvable by Haiku in under 25 turns
    when briefed well, and comes with hidden tests.
  - For each task, write 2–3 **planted facts**: constraints or decisions stated only in
    the parent prompt (e.g. "name the new constant `RETRY_CEILING`", "do not modify
    `utils/io.py`"), each with a hidden test that checks it.
- **Accept:** hidden tests fail on the untouched fixture, pass on a reference solution
  (kept outside the fixture dir), and planted-fact tests fail on a solution that ignores
  the facts.

### R1 · Brief receipts joiner
- **Agent:** Sonnet · **Files:** `cfxd/brief_receipts.py` (new), tests
- **Do:** join `briefs.jsonl` entries to child transcripts (sidechain / sub-agent
  sessions) and compute per brief:
  - packet bytes and items
  - the target model
  - which briefed files the child re-read anyway
  - which files the child read that the parent had already read but weren't briefed
  - child tokens (dedup counted, `cfxd/usage_dedupe.py`), child compactions and
    overflows, outcome
- **Accept:** fixture transcripts with a known answer; works on real Claude Code sub-agent
  transcripts (include one recorded sample).

## Wave 3

Split Brief so two agents can work in parallel without sharing a file.

### B1 · Model budgets and relevance ranking
- **Agent:** Sonnet · **Files:** `cfxd/brief_select.py` (new), tests
- **Do:** `select(facts, prompt, target_model) -> Selection`. Budget comes from ADR-0011.
  Rank files by:
  - paths and symbols named in the prompt (exact, then basename, then module)
  - files the parent edited
  - import neighbours of those (static parse, fixture-safe)
  - recency

  Return ordered items with reasons.
- **Accept:** table-driven tests (a prompt naming `foo/bar.py` ranks it first; a Haiku
  budget yields fewer items than Sonnet; deterministic output).

### B2 · Excerpts and packet v0 rendering
- **Agent:** Sonnet · **Files:** `cfxd/brief_render.py` (new), tests
- **Do:** render a `Selection` into packet v0 plus a text block for injection.
  - Excerpts are the line ranges the parent read or edited, read from disk at brief time,
    hash-checked. A changed file gets a pointer only.
  - The budget is enforced per section; v0's sanitiser is reused.
- **Accept:** the round-trip validates against packet v0 schema; caps hold under
  adversarial paths and contents; injection strings in files cannot close the delimiter.

## Wave 4

### B3 · Intent, constraints and carry-back; wire into the hook
- **Agent:** Sonnet · **Files:** `cfxd/brief.py`, `cfxd/brief_intent.py` (new),
  `cfxd/config.py` (add `brief_v1`, `carry_back` flags), tests
- **Do:**
  - Extract goal, constraints, decisions and already-tried from parent messages and the
    delegation prompt (rule-based first: imperative constraint patterns, "don't/must/use",
    named identifiers).
  - Wire B1 + B2 into the PreToolUse handler behind `brief_v1`; v0 stays as fallback.
  - Carry-back: on sub-agent completion, append a capped summary (files touched, result,
    open issues) for the parent.
  - Log receipts in R1's format.
- **Accept:** tests seed injection text in tool results and assert it never appears in a
  packet; end-to-end with the fake claude produces a v1 packet for a Haiku child within
  budget; v0 behaviour is unchanged when the flag is off.

### H2 · v4 harness arms
- **Agent:** Sonnet · **Files:** `evals/plugin_ab/runner.py` (arm + delegation parts),
  `evals/plugin_ab/analyze.py`, tests
- **Do:**
  - Add arms `none`, `brief_v0`, `brief_v1`, `fork`, `control`, configured via
    `_write_arm_config`.
  - Write the parent prompt template that plans and delegates to Haiku sub-agents.
  - Record per run: parent and child usage (dedup), spawn count, child compactions,
    hidden-test pass, planted-fact pass, R1 receipts.
  - Keep the A1 MCP assert. A run with zero spawns is recorded as `no_spawn` (data, not
    infra).
- **Accept:** fake-claude runs for each arm produce complete records; analyzer outputs
  primary and secondary tables from fake data.

## Wave 5

### P0 · v4 pilot (GATE)
- **Who:** founder runs it in cf-bench; Opus reads the transcripts.
- **Run:** 2 tasks × 5 arms × 1 rep, Sonnet parent, Haiku children, cap ≤ $10 total.
- **Check:**
  - the spawn rate per arm
  - that `control` fails planted facts
  - zero `mcp_churn`
  - real packet sizes vs budgets
  - per-run cost
  - anything odd in the transcripts (read at least one per arm)
- **Output:** `docs/plans/v4-pilot-notes.md` with budget tuning and fixes (fixes become
  small WPs before H3).

## Wave 6

### H3 · Freeze the v4 protocol
- **Agent:** Opus · **Files:** `evals/plugin_ab/PROTOCOL_V4.md`,
  `evals/plugin_ab/prereg_v4.sha256`
- **Content:**
  - Hypotheses, primary outcome (total cost at non-inferior pass rate, `brief_v1` vs
    `none`) and secondary outcomes.
  - Arms, tasks, repetitions (sized from the pilot), interleaved ABBA order.
  - Exclusions: infrastructure only (`mcp_churn`, launch failures). `no_spawn` is data.
  - The cost cap and the analysis code path.
- **Accept:** SHA committed before the measured run. The founder starts the overnight run.

### U1 · `cfxd brief` receipts view
- **Agent:** Sonnet · **Files:** `cfxd/cli.py` (brief subcommand only),
  `commands/brief.md`, tests
- **Do:** `cfxd brief [--session]` lists each handoff: target model, packet size, the
  top items and why they were chosen, and what the child re-read anyway. It's the tester
  feature that makes Brief visible.
- **Accept:** output on a recorded session matches R1 numbers.

### X1 · Cross-agent brief demo
- **Agent:** OpenCode · **Files:** `docs/demos/brief-handoff.md`,
  `scripts/demo_brief_handoff.*`
- **Do:** Claude parent → Brief v1 packet → `cfxd handoff` → Codex/OpenCode child on a
  fixture, with the expected output shown.
- **Accept:** runs on Windows and in macOS CI.

## Wave 7

| WP | Agent | Files (lock) | Do | Accept |
|---|---|---|---|---|
| **C2** release + install | OpenCode | `.github/workflows/release.yml`, `scripts/install.*` | tag → build → GitHub release; one-line install for Windows/macOS/Linux; `cfxd doctor` first | install to first brief receipt < 5 min, timed on CI |
| **T1** tester guide | OpenCode | `docs/TESTING-GUIDE.md`, `.github/ISSUE_TEMPLATE/` | install → doctor → work normally with sub-agents for a week → `cfxd brief` → `week --share` | founder walkthrough on a clean profile |
| **F1** fidelity command | Sonnet | `cfxd/fidelity.py`, `cfxd/cli.py` (fidelity only; run after U1 merges) | "what did your /compact forget?" on the user's own logs | matches frozen metric definitions; < 10s on 50 sessions |

## Waves 8–10

- **A8** v4 analysis exactly as registered · Opus · `evals/reports/v4_registered/`,
  deviations if any. Read transcripts from each arm before trusting the tables.
- **PP** paper skeleton · Opus · `paper/`. Sections:
  - thesis (handoff waste)
  - method (harness, dedup, MCP isolation, planted facts)
  - v4 results
  - motivation from fidelity (21% median) and refetch
  - threats (one fixture, one machine, Sonnet as the "big" model)

  Every number comes from a script; extend the docs-numbers checker to `paper/`.
- **D2** README · Opus · last. Lead with Brief; only claims v4 supports.
- **D3** post draft · Opus · `docs/drafts/`. Unpublished until A8 is done.

## Day map

| Day | Assign |
|---|---|
| 2 | B0, H1, R1 |
| 3 | B1, B2 |
| 4 | B3, H2 |
| 5 | P0 (gate) + pilot fixes |
| 6 | H3 → overnight run; U1, X1 |
| 7 | C2, T1, F1 |
| 8 | A8 |
| 9 | PP |
| 10 | D2, D3, tester handoff |
