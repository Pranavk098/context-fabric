# Orchestration plan: ten-day sprint as agent work packages

Companion to `2026-10-29-ten-day-plan.md` (the why). This file is the how. Each work
package (WP) is self-contained so it can be pasted into an Orca task and handed to a Claude
Code or OpenCode agent.

## Ground rules for every agent

Paste this block at the top of every task.

```
Repo: github.com/Pranavk098/context-fabric. Work on your own branch `s10-<wp-id>` from main.
Read CLAUDE.md / AGENTS.md first. Python, uv, ruff, pytest.
Before you finish: `uv run ruff check .`, `uv run pytest -q`, and `uv run python scripts/check_docs_numbers.py` must pass.
No new public claims. Do not edit README headline numbers, registered protocols (PROTOCOL*.md),
or anything under evals/reports/. Touch only the files your WP lists; if you need another, stop and say so.
Commit with a clear message; do not push or merge. End with: files changed, tests added, commands run + output, open questions.
```

- `C:\dev\cf-bench` is a second clone used **only for running benchmarks**. Agents write
  code in worktrees of the main repo. Pull into cf-bench just before a run.
- One writer per file set: the "Files" column is the lock. Two WPs that share files never
  run at the same time.
- I (the founder) merge. The coordinator agent reviews each branch against its acceptance
  checks before merging.

## Agent routing

| Agent | Use for |
|---|---|
| **Claude Opus (coordinator)** | Reviews, merges, protocol and registration text, paper sections, anything statistical |
| **Claude Sonnet** | Most implementation WPs with clear acceptance tests |
| **OpenCode** | Mechanical or well-specified WPs (CI YAML, install script, adapters copied from existing readers, docs). Note: `--session` resume runs headless; give it the full brief each time |

## Dependency graph

```
Wave 1 (day 1, parallel):  A1 MCP isolation   A2 usage dedup   C1 macOS CI   D1 ADRs + B8 note
Wave 2 (day 2):            A3 recall redesign + 0-byte guard  (after A1)
                           A4 re-analyze v2 with dedup         (after A2)
                           A5 off≈shipped probe                (after A1+A2)  ← gate
Wave 3 (day 2-3):          A6 register v3                      (after A3, A5 passes)
                           A7 run v3 overnight (founder, cf-bench)
Wave 4 (days 3-8, parallel, independent file sets):
                           F3 churn rule   F4 checkpoint hardening   F1 fidelity command
                           F2 report card (after F1, F3: reuses their outputs)
                           F5 handoff demo   F6 public bench docs (after A6)   F7 tester kit
                           C2 release + install script (after C1)
Wave 5 (days 7-10):        P1 paper methods+fidelity   P2 v3 analysis (after A7)
                           D2 README rewrite (last; after P2)   D3 post draft (not published)
```

Gate: **if A5 shows off ≠ shipped after the fixes, stop Wave 3.** Open a debugging WP
(bisect hooks on/off) before any v3 registration.

---

## Wave 1

### A1 · MCP isolation in the benchmark harness
- **Agent:** Sonnet · **Files:** `evals/plugin_ab/runner.py`, `tests/` (new test file)
- **Context:** In v2, account MCP connectors (~490 tools) leaked into runs despite
  `--setting-sources project,local`. Off's first turn raced them: `deferred_tools_delta`
  attachments appeared 27× per run in off vs 1× in shipped, adding ~30K cache writes per
  resumed step. Evidence: transcripts for sessions `7e5be006…` (off) and `1d4c469f…`
  (shipped) under `~/.claude/projects/*plugin-ab-3ma7t17u-long-checkout*`.
- **Do:**
  1. In `build_command`, add `--strict-mcp-config --mcp-config <path to an empty
     {"mcpServers":{}} file written into the run dir>` for **all arms**.
  2. Set the env var that disables claude.ai account connectors for the subprocess, if the
     installed Claude Code supports one. Check `claude --help` and the docs; if there is
     none, say so and rely on step 1.
  3. After each run, count `deferred_tools_delta` and `mcp_instructions_delta` attachments
     in the transcript. If either is more than 1 per invocation-start, record an infra
     failure with reason `mcp_churn`, regardless of arm.
- **Accept:** a unit test with a fake transcript containing churn → infra failure. A
  command-builder test asserts the flags are present for off, shipped and checkpoint.
  `pytest` is green.

### A2 · Count usage once per message
- **Agent:** Sonnet · **Files:** whichever module sums transcript usage for plugin_ab
  (find it from `runner.py` / `analyze.py`; likely in `evals/plugin_ab/` or a `cfxd` usage
  reader), plus tests
- **Context:** Claude Code writes one JSONL line per content block, and each line repeats
  `message.usage`. Steps are currently summed per line. Proof: off "tax" step reported
  27,630 cache writes = 1035×2 + 5704×4 + 2744.
- **Do:** deduplicate by `message.id` (keep the last line's usage per id) everywhere
  session usage is summed: the benchmark, and any `cfxd` command (`stats`, `diet`,
  `waste`, `ledger`, `week`) that reads the same transcripts. List every call site you
  changed.
- **Accept:** a regression test built from the tax-step lines yields 9,483 cache writes,
  not 27,630. Existing tests are updated with a note on any expected-value change. The
  docs-numbers checker passes, or the agent lists exactly which published figures move.
  **Do not edit the figures themselves; report them.**

### C1 · macOS CI
- **Agent:** OpenCode · **Files:** `.github/workflows/` (new `macos.yml`)
- **Do:** a job on `macos-latest` that runs `uv sync`, ruff, pytest, plus a smoke step:
  install the plugin from the checkout, run `cfxd doctor`, and run `cfxd` against a
  fixture transcript. Mark tests that are known Windows-only.
- **Accept:** a workflow run link with a green result, or a list of the failing tests by
  name (that list is itself the macOS support report).

### D1 · Decisions on paper
- **Agent:** Opus · **Files:** `docs/adr/0001-compiler-vs-plugin.md`,
  `docs/adr/0002-contextdb.md`, `docs/decisions/b8-parked.md`
- **Do:** short ADRs (context, decision, consequences). Proposed defaults, which the
  founder confirms:
  - The plugin is the product; the compiler is internal.
  - ContextDB is out of scope until after v3.
  - B8 is parked with its re-entry criteria.
- **Accept:** founder sign-off in the PR.

## Wave 2

### A3 · Recall that actually measures memory, plus a save guard
- **Agent:** Sonnet (Opus reviews) · **Files:** `evals/plugin_ab/runner.py` (checkpoint
  and recall parts only, so it runs after A1 merges), `evals/plugin_ab/fixture_v2/` task
  defs, tests
- **Do:**
  1. Recall questions ask about facts planted mid-session that exist nowhere in the repo
     (a random token, an invented decision). Generate them from the seed per run.
  2. Add a `no_restore` control arm used only for recall validation. Any question it
     answers correctly gets flagged invalid.
  3. A checkpoint save that returns 0 bytes marks the run failed (`save_empty`), never
     degraded.
- **Accept:** tests for each. A pilot of 2 runs on fake claude shows the control scoring
  0.

### A4 · Re-analyze v2 with dedup (exploratory)
- **Agent:** Opus · **Files:** `evals/reports/v2_registered_dedup/` (new),
  `docs/deviations/…v2.md`
- **Do:** recompute all v2 ratios from the raw transcripts with A2's counting. Write the
  deviation entry: registered results void for off-based ratios (MCP race + double count),
  dedup results labelled exploratory. The checkpoint-vs-shipped ratio is reported but not
  claimed.
- **Accept:** a table of old vs dedup ratios per stratum. No README change.

### A5 · Gate probe: off ≈ shipped?
- **Agent:** founder runs it, Sonnet prepares the command · **Where:** cf-bench, after
  pulling A1+A2
- **Do:** 1 long task × off/shipped × 3 reps, interleaved, Haiku.
- **Pass:** shipped/off between 0.9 and 1.1, and zero `mcp_churn` failures. **Fail:** stop
  and open WP A5b (bisect: plugin loaded with hooks disabled vs enabled).

## Wave 3

### A6 · Register v3
- **Agent:** Opus · **Files:** `evals/plugin_ab/PROTOCOL_V3.md` (new; leave the existing
  `PROTOCOL_V3_EXPLORATORY.md` untouched), a SHA entry
- **Content:**
  - Primary comparison: checkpoint vs shipped. Off is secondary.
  - Fixtures v2 + v3, interleaved ABBA arm order.
  - The A1 MCP assert, A2 counting, A3 recall + control + save guard.
  - Strata, and the number of tasks per stratum (aim for ≥5 long).
  - Thresholds and the cost cap.
  - Exclusions limited to infrastructure failures (including `mcp_churn` and
    `save_empty`).
- **Accept:** SHA recorded before any measured run.

### A7 · Run v3 · founder, cf-bench, overnight. Log the run id and timings in the decisions file.

## Wave 4: product features (parallel; separate file sets)

| WP | Agent | Files (lock) | Do | Accept |
|---|---|---|---|---|
| **F3** churn rule | Sonnet | `cfxd/waste.py` + tests | New waste rule: count tool-list / MCP-instruction re-announcements per session and estimate their tokens; show "N re-announcements ≈ X tokens"; suggest `--strict-mcp-config` / fewer connectors | fires on the v2 off transcript, silent on shipped |
| **F4** checkpoint hardening | Sonnet | `cfxd/checkpoint.py`, `commands/` checkpoint files, tests | empty save = loud error; restore verifies hash; `cfxd checkpoint status`; doctor check | tests for empty / corrupt / missing; doctor shows status |
| **F1** fidelity command | Sonnet | `cfxd/fidelity.py`, `cfxd/cli.py` (fidelity subcommand only), tests | `cfxd fidelity` on the user's own logs: per compaction, files held vs named vs dropped vs invented; plain-language summary + JSON | runs on a fixture; matches the frozen metric definitions; under 10s on 50 sessions |
| **F2** report card | Sonnet (after F1, F3) | `cfxd/report.py` (new), template, tests | `cfxd report --html`: one self-contained page with token breakdown, waste callouts, fidelity, light/dark | numbers equal the CLI outputs (test); opens offline |
| **F5** handoff demo | OpenCode | `docs/demos/handoff.md`, `scripts/demo_handoff.*` | scripted Claude → packet → Codex/OpenCode flow on a fixture, with expected output | script runs clean on Windows + macOS CI |
| **F6** public bench | OpenCode (after A6) | `evals/plugin_ab/README.md`, `Makefile` target | `make bench-pilot` reproduces a small run on someone else's machine; documents MCP isolation and cost | fresh clone → pilot completes |
| **F7** tester kit | OpenCode | `docs/TESTING-GUIDE.md`, `.github/ISSUE_TEMPLATE/` | install → doctor → use for a week → `fidelity` + `report` → `week --share`; feedback template | founder walkthrough on a clean profile |
| **C2** release + install | OpenCode (after C1) | `.github/workflows/release.yml`, `scripts/install.*` | tag → build → GitHub release; one-line install for Windows/macOS/Linux; `cfxd doctor` first | install to first report < 5 min, timed on CI |
| **O1** OpenCode/Codex adapters | Sonnet | `adapters/` | plugin/hook adapters reusing the existing log readers; `stats`/`fidelity` work on their logs | fixtures from each tool pass |

Run O1 only if Wave 4 is on schedule by day 6; otherwise it's first after testers start.

## Wave 5

- **P1** paper methods + fidelity results · Opus · `paper/` (new): data, frozen metrics,
  fidelity (21% / 460 of 614 / 87 of 841), refetch null, threats to validity (n=1, single
  machine). Every number must come from a script in the repo, and the docs-numbers checker
  must cover `paper/`.
- **P2** v3 analysis exactly as registered · Opus · `evals/reports/v3_registered/`,
  deviations if any.
- **D2** README rewrite · Opus · last. The one-sentence contribution, only proven claims,
  quick start = C2 install, links to fidelity + report card.
- **D3** post drafts · Opus · `docs/drafts/` ("What /compact forgets"; "MCP tool-list
  churn"). Not published until P2 is done and churn is reproduced on a second machine.

## Daily coordinator loop (Opus in Orca)

1. Morning: check which WPs are unblocked per the graph, assign them, and paste the
   ground rules + WP block.
2. On each finished branch, verify the acceptance checks yourself (run the commands; don't
   trust the report), then hand it to the founder to merge.
3. Evening: update `docs/plans/STATUS.md` with WP → state, branch, blockers, and the next
   wave.
4. Never run two WPs with overlapping Files at once. Never start A6 before A5 passes.

## Day map

| Day | Assign |
|---|---|
| 1 | A1, A2, C1, D1 |
| 2 | A3, A4, A5 (gate) |
| 3 | A6 → A7 overnight; F3, F4 |
| 4–5 | F1, C2, F7 |
| 6 | F2, F5, (O1 if on time) |
| 7 | P1, F6, P2 when v3 is done |
| 8–9 | fixes, D2 |
| 10 | buffer, D3, tester handoff |
