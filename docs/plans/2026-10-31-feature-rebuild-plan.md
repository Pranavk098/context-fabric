# Feature rebuild plan (2026-10-31)

What to build next for every feature that is disproven, broken or unproven, and what
replaces the disproven ones. Paid model runs and Gate 2 are **on hold** (Claude weekly
usage at 76%). Everything here is offline: built and checked against real logs and unit
tests, with no model spend. The paper material is in `docs/paper/EVIDENCE-DUMP-2026-10-31.md`.

## Principle

Every feature gets a **free offline proxy** that predicts whether it can work before any
paid run. Real transcripts (991 sub-agent transcripts, 28+ compactions, 852 sessions)
already contain the answer to "would this have helped?" for most features. A feature
passes its offline proxy first; paid runs only confirm it.

## Feature verdicts and what replaces them

| Feature | Verdict | Keep / change / replace | Offline proxy that decides it |
|---|---|---|---|
| **Brief v1** | Unproven; Haiku segment has ~0 to save (Gate 1) | **Change**: retarget to expensive children and parents with real knowledge; skip where the expected value is ~0 | **Brief replay scorer** (R-1): for each real spawn, rebuild the packet the parent *would* have sent and measure how many of the child's actual redundant reads it covers, at what byte cost |
| **Checkpoint** (restart + restore) | Disproven: 1.25–1.37× cost (it restarts, re-pays the prefix and loses the cache) | **Replace** with **compaction carry v2**: no restart; improve what survives `/compact` via the existing `PreCompact`/`SessionStart(compact)` path (`cfxd/carry.py`) | **Carry fidelity scorer** (R-4): on real compactions, how many of the dropped edited files (460 of 614) and held files the carry block restores, at what bytes |
| **Fold** (Read/Bash output) | Disproven (+101% on Read; no saving on Bash) | **Retire** fold; keep the code, off. Its goal (big tool outputs) moves to **bound** | none; closed |
| **Bound** (tail of noisy Bash output) | Untested, off | **Keep, measure offline**: replay real Bash outputs through bound's rewrite rules | **Bound replay** (R-6): tokens saved on real long outputs, and how often the dropped head contained an error line (the risk) |
| **Coach** (nudge on long, expensive sessions) | Untested, off | **Keep, measure offline**: how often it would have fired, and at what point in the sessions that turned out to be the costly 10% | **Coach replay** (R-6): precision/recall of the nudge against the top-10% cost sessions |
| **Carry-back** (child → parent summary) | Broken: async children return `async_launched`, so the PostToolUse hook never sees the result | **Change**: trigger on child completion instead (a `SubagentStop`-style hook, or the background-task completion notification); verify the payload first | unit tests on recorded payloads; a dry-run summary built from real child transcripts |
| **Diet** (trim unused skills/connectors) | Works as a report; `--apply` exists; saving unmeasured | **Promote**: the one saving that is deterministic. Prefix tokens removed are re-read on every turn | **Diet delta** (R-5): exact prefix-token reduction per turn for the user's setup, and its cache-read dollars over their real sessions |
| **MCP churn** | New finding, not a feature yet | **Add** as a `waste` rule and a `doctor` warning | fires on v2 off transcripts, silent on shipped |
| **Routing** (model tier per child) | Report exists (price only) | **Extend**: per-child-model re-discovery from the Gate 1 join; flag Opus children that look like research/exploration | report on real logs; price only, quality stays unmeasured |

## Alternatives considered for the disproven items

- **Checkpoint → compaction carry v2 (chosen).** Fidelity shows compaction drops 75% of
  edited files, and refetch shows agents rarely re-read after compaction. So the loss
  shows up as lower work quality, not extra cost. Carry already injects facts after
  compaction. v2 makes it carry the right ones: edited files first, with line ranges, the
  last test result, open todos and the user's constraints (reusing Brief v1's intent
  extraction). It needs no restart and no new session, so no extra prefix cost.
- **Checkpoint → custom compact instructions (backup).** Hand `/compact` a short list of
  must-keep items. Only worth it if Claude Code exposes a supported way; verify before
  building.
- **Fold → none.** The 7% upper bound on shrinking old tool results (852 sessions) says
  the whole category is small. Don't re-invest.
- **Brief for Haiku children → skip.** Gate 1 shows 0.5–0.9% to save. Brief should stay
  silent there, which also makes it cheaper and safer.
- **Brief retarget → model-tier hint (later, needs a paid run).** If a good brief lets a
  Sonnet child do an Opus child's job, the saving is the price gap, not the re-reads. A
  hypothesis only; not in this plan.

## Work packages (all offline, OpenCode-sized)

Each WP lists the files it may touch (its lock), what to do, and the acceptance check.
Run ID prefix `s12-`. Shared rules are in `2026-10-31-orca-run.md`.

### R-1 · Brief replay scorer
- **Files:** `scripts/brief_replay_score.py` (new), `tests/test_brief_replay_score.py`
  (new), `docs/brief-replay-score.md` (generated output)
- **Do:**
  - Reuse the Gate 1 join (`scripts/gate1_rediscovery.py`: parent `Agent` tool_use →
    `toolUseResult.agentId` → child transcript).
  - For each joined child, rebuild the parent's `SessionFacts` from the parent transcript
    up to the spawn, then call `cfxd.brief_select.select` with the child's real prompt and
    model.
  - Report, by child model family and by "parent knew ≥3 files":
    - **coverage**: share of the child's redundant reads whose path is in the packet
    - **precision**: share of packet files the child actually read
    - packet bytes
    - estimated tokens avoidable (the Gate 1 lower/upper method restricted to covered
      reads)
- **Output rule:** aggregates only, no paths or text (same rule as Gate 1).
- **Accept:** unit tests on 2 synthetic parent/child transcript pairs with known
  coverage; the script runs on real logs in < 5 minutes and writes the doc.

### R-2 · Brief policy (skip where it can't help)
- **Files:** `cfxd/brief.py` (policy check only), `cfxd/config.py` (add `brief_policy`,
  default on), `tests/test_brief_policy.py` (new)
- **Do:** when `brief_policy` is on, skip briefing if the target model is Haiku or the
  parent has fewer than 3 known files. Log `decision: "skipped"` with reason
  `policy_haiku` / `policy_low_knowledge`.
- **Accept:** tests for each reason; v0/v1 behaviour unchanged when the policy is off.
  `cfxd brief` shows the skip reasons.

### R-3 · Carry-back on child completion
- **Files:** `docs/research/subagent-completion-hooks.md` (new), `cfxd/carry_back.py`
  (new or moved out of `brief.py`), `hooks/hooks.json` (one new entry),
  `tests/test_carry_back.py`
- **Do:**
  1. Research step, written down first: which Claude Code hook fires when a sub-agent
     (including background/async) finishes, and its payload fields (agent id, transcript
     path, result). Cite the doc URL for each field. If nothing reliable exists, stop
     after the doc.
  2. If one exists, build the capped summary (files touched, result line, open issues)
     from the child transcript and attach it for the parent, behind `carry_back`.
- **Accept:** the research doc with citations; tests on recorded/synthetic payloads; off by
  default.

### R-4 · Compaction carry v2 + fidelity scorer
- **Files:** `cfxd/carry.py`, `scripts/carry_fidelity_score.py` (new),
  `tests/test_carry_v2.py` (new), `docs/carry-fidelity-score.md` (generated)
- **Do:**
  - Carry block v2 orders edited files first (with line ranges from `FileFact.ranges`),
    then the last test result, open todos, and the user's constraints via
    `cfxd.brief_intent.extract_intent`.
  - Cap: 4 KB. Keep the existing safety rules: content-free facts and no tool output.
  - Scorer: on each real compaction, compare against the frozen fidelity metrics. How
    many dropped edited files and held files would carry v1 vs v2 restore, and at how
    many bytes?
- **Accept:** tests; the scorer reports v1 vs v2 restore rates on real compactions;
  `docs/FIDELITY-METRICS.md` definitions unchanged.

### R-5 · Diet delta (deterministic saving)
- **Files:** `cfxd/diet.py` (report section only), `tests/test_diet_delta.py` (new)
- **Do:** for the `--apply` plan, compute the exact prefix tokens removed per turn (from
  the listing sizes diet already measures). Then multiply over the user's real sessions
  (turns × removed tokens) and price it as cache reads and the share of cache writes.
  Show it in `cfxd diet` as "would remove N tokens from every turn; about $X over your
  last 30 days at list price".
- **Accept:** tests with a synthetic setup; numbers equal the sum of item sizes; labelled
  an estimate.

### R-6 · Bound and coach replay
- **Files:** `scripts/bound_coach_replay.py` (new), `tests/test_bound_coach_replay.py`
  (new), `docs/bound-coach-replay.md` (generated)
- **Do:**
  - Bound: replay real Bash tool results through bound's rewrite rules, and report the
    tokens saved plus the share of rewritten outputs whose dropped head contained
    `error|Error|FAILED|Traceback`.
  - Coach: replay its Stop-hook trigger over real sessions, and report precision and
    recall against the top-10% cost sessions, plus the median turn at which it would have
    fired.
- **Accept:** tests on synthetic transcripts; aggregates only.

### R-7 · MCP churn rule + doctor warning
- **Files:** `cfxd/waste.py` (new rule only), `cfxd/cli.py` (doctor warning only; run
  after R-8 merges), `tests/test_waste_mcp_churn.py` (new)
- **Do:** count `deferred_tools_delta` / `mcp_instructions_delta` attachments per session
  and estimate their tokens. Warn when there is more than 1 per invocation start, and
  suggest `--strict-mcp-config` or fewer connectors for headless runs.
- **Accept:** fires on a synthetic churn transcript; silent on a clean one.

### R-8 · Housekeeping: retire, fix, document
- **Files:** `cfxd/cli.py` (`_BRIEF_EMPTY` text only), `docs/TESTING-GUIDE.md`
  (troubleshooting section only), `tests/test_plugin_ab.py` (env dict fix only),
  `docs/decisions/ADR-0012-retire-fold-and-checkpoint-restart.md` (new)
- **Do:**
  1. `_BRIEF_EMPTY`: print "Turn it on with…" only when Brief v1 is off.
  2. Guide: if Windows blocks `cfxd.exe` (Application Control), run `python -m cfxd …`
     instead.
  3. Tests: replace `dict(**os.environ, PYTHONDONTWRITEBYTECODE="1")` with
     `{**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}`, so the tests pass when the shell
     already exports it.
  4. ADR: fold retired, checkpoint-restart superseded by carry v2, with evidence links.
- **Accept:** the 9 previously failing local tests pass with `PYTHONDONTWRITEBYTECODE=1`
  exported; ruff and the docs-numbers check pass.

### R-9 · Routing by child model
- **Files:** `cfxd/routing.py` (child-model section only), `tests/test_routing_children.py`
  (new)
- **Do:** split sub-agent cost by child model family and show the Gate 1 re-discovery share
  per family when transcripts allow it. Price only, with "quality unmeasured" in the
  output.
- **Accept:** tests on synthetic data; the output matches `docs/gate1-rediscovery.md`'s
  family table on the same logs, within rounding.

## Decision rules for when the hold lifts

- **Brief (Gate 2)** goes ahead only if R-1 shows coverage ≥ 50% of redundant reads for
  Opus/Sonnet children whose parent knew ≥3 files, at ≤ 6 KB. Otherwise, improve
  selection first (Aider-style weights, artifact references) and re-score. It's free.
- **Carry v2** gets a paid confirmation only if R-4 shows it restores at least twice as
  many dropped edited files as v1.
- **Bound/coach** get turned on for testers only if R-6 shows real savings and that
  bound rarely drops error lines.
- **Diet** can be described to testers now as an estimate. It's arithmetic, not a claim
  of quality.
