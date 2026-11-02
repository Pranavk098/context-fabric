# Plugin waves: Orca build plan (2026-11-01)

Hand this file to Orca. It turns the Plugin Sprint Plan and the Plugin Technical Plan (P1–P12) into
four waves of parallel work, plus the rules every agent follows. It supersedes nothing in those two
documents except where section 1 says they are stale against the repo.

---

## 1. Where the plan stands against the repo (verified 2026-11-01 at `66db9e5`)

| PDF says | Repo says | Consequence |
|---|---|---|
| "No tagged tester build yet (M-11)" | Tag `v0.1.1-alpha.1` is on origin at `66db9e5`; `install-smoke` (ubuntu, macos, windows matrix) and `release` ran green on that tag | P1 is not "create a tag" any more. It is "ship `v0.1.1-alpha.2` with the P2/P3/P8/P12 deltas". Never move or reuse `alpha.1` |
| Coach ships with the random-half control | `coach_control` defaults to `False` in `cfxd/config.py` | The control does not run unless a tester turns it on, so P11 collects nothing. **Decided D-1: on, with disclosure** (section 3; W1-06 flips it) |
| SECURITY.md stale on carry size (3,000 vs 4 KB) | SECURITY.md already states v1 3,000 and v2 4,000 bytes | Text fixed. What is still missing is the CI check that ties the doc number to the code constant (P3) |
| Share JSON "never vouched for" | `tests/test_share_numeric.py` covers text `--share` with canaries; no `cf-share/2` schema, no allowlist serializer, no `plugin_version` in payloads | P2 is real work |
| Worker template reads `AGENTS.md` | No `AGENTS.md` or `CLAUDE.md` in the repo | W0 adds a short `AGENTS.md` (section 6 rules, verbatim) |
| "Any CLI" (the founder's goal) | Hooks are Claude Code only. Every log reader reads `~/.claude/projects`. `adapters/` holds LangGraph and raw-LLM only | Nothing works for Codex, Gemini CLI or OpenCode today. Section 2, item 1 |

## 2. Assessment: agree, with six changes

**Agree.** The two documents are the right plan. Freezing the plugin, measuring before building,
frozen bars written before the run, the allowlist share serializer, "hash equality authorises
elision", the tracker as pure functions, and pre-written fallbacks are what make this credible. The
one rule worth repeating to every agent: *P5 and P7 do not start until P4 numbers exist.*

**Change 1: say plainly that this is a Claude Code plugin, and add measurement-only readers for
other CLIs.** Hooks cannot be ported cheaply (Codex, Gemini CLI and OpenCode each have different or
no hook surfaces). Session logs can be read. Add one log-reader interface (`cfxd/logs/`) with a
Claude Code reader (existing code moved behind it, no behaviour change) and, if W1 research confirms
the formats, a Codex reader. Then `audit`, `week` and the P4 measurements work on Codex logs. The
README states "hooks: Claude Code only; reports: Claude Code, and Codex (beta)". Do not claim
any CLI whose reader has no golden fixture.

**Change 2: give testers one honest, measurable saving now: diet apply, before and after.** Every
conservation mechanism is still unproven, but diet apply removes unused MCP servers and skills from
the prefix every turn pays. That is measurable on the tester's own machine with no model spend:
record the first-turn prefix size of the next N sessions before and after apply. Label it
*observed (one machine)*, never *measured*. This is the only "saves context" sentence the tester
build can truthfully print, and it gives testers a reason to stay. (WP W1-08.)

**Change 3: aim P5 at Bash/test output, not Read.** Claude Code already stubs unchanged Reads.
P4 M2 will likely confirm it; build the sub-range path first if M1 clears.

**Change 4: budget the paired run before anyone promises it.** The plan's 20–30 task pilot and
100+ task paired run cost real model spend. Write the budget and the stop rule into the
pre-registration (W3) or the "no saving is proven" line never moves.

**Change 5: accountability through receipts on the build itself.** Every results note cites the
commit that froze its bars; every share payload carries `plugin_version` and `metric` version;
every WP's final message carries the exact commands and last output lines; the orchestrator re-runs
them. Nothing is "done" on a builder's word.

**Change 6: one owner for `cfxd/cli.py` per wave.** Most WPs need a CLI flag. If each edits
`cli.py`, waves serialise on merge conflicts. Rule: feature WPs expose a function and a test; one
wiring WP per wave owns `cli.py`.

**Deployability verdict.** Installable today on Linux, macOS and Windows from a tag, with CI proof.
Not yet *useful enough to keep*: the headline is raw tokens (P8 missing), share JSON is not
allowlisted (P2), the coach experiment is off (D-1), and nothing the tester sees saves context
except diet. Waves 1–2 fix all four. Cross-CLI is a wave 3 item at the earliest.

## 3. Founder decisions (decided 2026-11-01)

| ID | Decision |
|---|---|
| D-1 | `coach_control` **on** by default in the tester build. Tester onboarding (W2-03) and the feedback form say plainly: "You are in a measurement study. Half of testers see coach prompts and half do not; which half you are in is fixed by a hash of your install." Consent to publish aggregates is not consent to the study, so the form asks both separately |
| D-2 | M3 bar: at least **25% of 40+ turn sessions have a break-even turn with positive headroom** (cost-to-finish restarted < cost-to-finish continuing, net of prefix re-pay and packet). Existence of a break-even turn alone does not count. This is gate 1 of 2 for P7; gate 2 is the coach treatment arm showing a behaviour difference in W4 |
| D-3 | Plan-tier denominators only from an official published source, labelled *estimate* with the source URL and date in `limit_weights.toml`. **Fallback: if no official figure exists for a tier, show limit-weighted tokens with no limit share for that tier.** An invented limit is never shown |
| D-4 | No number chosen blind. W3-05 runs a 3-task probe (v4 pilot precedent) per arm; ceiling = probe cost per task × tasks × arms × 1.5, written into the pre-registration with the stop rule: stop at the ceiling, report partial results, no silent overrun |
| D-5 | Codex reader ships behind `--source codex`, labelled beta, with a golden fixture, **only if W1-09 returns "reader feasible" with real per-message usage data.** If W1-09 returns "partial (no usage)", the reader reports counts only with no cost or limit figures, or it does not ship |

**P4 bars, frozen** (committed in W0 before any measurement runs; this file does not move them):

| Result | M1 (total elidable repeat-share of new tokens) | Consequence |
|---|---|---|
| ≥ 15% | lead | W2-05 tracker and W3-03 dedupe build as a feature; dedupe may lead the tester narrative after the paired run |
| 5–15% | quiet | W2-05 and W3-03 build **only as off-by-default experiments with no tester claim**, not in any guide or README headline |
| < 5% | demote | W2-05 and W3-03 park; dedupe becomes a framework-track experiment only |

M3 uses D-2. W2-05 also starts if M3 clears D-2 even when M1 < 5% (the tracker serves P7's packet).

The orchestrator commits these decisions and bars to `docs/plans/2026-11-01-wave-decisions.md`
**before** W1 dispatch. The commit hash is cited in every P4 results note. A changed bar after that
commit is a new, dated decision note, never an edit.

**The tracker (W2-05) is not a framework dependency.** If it parks, the framework track is not
blocked; its tracker just has no plugin prototype to learn from.

## 4. Roles

| Role | Who | Does | Never does |
|---|---|---|---|
| **Orchestrator** | Claude **Sonnet**, one Orca session | dispatch, file-lock table, re-run every acceptance check on each branch, line-by-line review of review-tier-1 code, merge green branches into `w-integration`, write the wave report | write feature code, approve its own security text, run paid model calls, push `main`, move a tag |
| **Builders** | **OpenCode** workers (model pinned in `~/.config/opencode/opencode.jsonc`; Orca rejects `--model` for OpenCode, so check the config first) | code, tests, scripts | touch files outside their WP, push, merge, edit bars or definitions |
| **Researchers / drafters** | **Antigravity** (Gemini) workers; if quota is out, Gemini CLI, else OpenCode | format research, doc and guide drafts | code in `cfxd/` (except research spikes in `spikes/`), security text approval |
| **Founder** | Pranav | D-1..D-5, approve SECURITY.md text, push, tag, tester outreach | — |

Max 5 workers running at once.

## 5. Dispatch, stall handling, merge

```
orca orchestration worker-start --worktree new-top-level --name w<wave>-<wp> \
  --repo path:C:/dev/Context-Fabric --base-branch w-integration --agent opencode
```

Use `--agent antigravity` for researcher WPs. Read the real branch inside each worktree with
`git branch --show-current` (Orca names branches after the active gh account).

**Stall protocol** (OpenCode workers loop on "title only" after about 30 minutes, usually with the
work finished):
1. At 30 minutes with no new commit, open the worktree: `git status`, `git diff --stat`.
2. If the diff covers the WP, run the WP's acceptance chain there yourself. Green: commit as
   orchestrator with `WP-ID (worker stalled; orchestrator-committed)`.
3. Red or partial: stop the worker, start a fresh worker on the same branch with the original prompt
   plus "Continue from the existing diff. The failing check is: <last 20 lines>".
4. Two failed restarts: park the WP in the wave report. Do not write it yourself.
5. **An orchestrator-committed diff gets the same review as a worker commit.** For tier 1 that is
   the full line-by-line review before merge; a green chain is not a review. The stall path is
   never a review bypass.

**Merge order** inside a wave: the order in the WP table. After each merge into `w-integration`,
run the full gate (section 6, G-FULL). A red full gate stops merging until fixed.

**Review throttle.** Most W1 WPs are tier 1 and one orchestrator reviews them all. If two or more
finished WPs are waiting for review, stop dispatching new WPs until the queue is back to one.
Unreviewed code never enters `w-integration`. Merge order is the throttle, not dispatch speed.

**Branch path to a release.** Workers branch from `w-integration`; the orchestrator merges into
`w-integration`. At the end of W2 the founder merges `w-integration` into `main` and pushes. **Tags
are cut only from `main`**, after CI on `main` is green, so install-smoke on the tag tests exactly
what users install. Never tag `w-integration` or a worker branch.

## 6. Rules for every agent (also committed as `AGENTS.md` in W0)

**Scope**
- R1. Touch only the files in your WP. Need another file? Stop and say why in your final message.
- R2. Python 3.12, `uv`. No new dependencies without the orchestrator's written OK.
- R3. No model or API calls in any code path, test or script. No network in tests.
- R4. Do not edit: `evals/plugin_ab/PROTOCOL*.md`, `prereg*`, `evals/reports/`, the README evidence
  table, `docs/FIDELITY-METRICS.md` definitions, `docs/engineering/NUMBERS.md`, frozen-bar files,
  golden files (unless your WP says so, and then the diff is reviewed line by line).
- R5. Never run a formatter over `evals/plugin_ab/fixture_v2/`.

**Privacy (P3 content classes)**
- R6. Anything that reads `~/.claude/projects`, `~/.codex` or any real log prints aggregates only:
  no paths, file names, prompts, commands, tool output, repo names, or timestamps finer than a day.
- R7. Nothing written to the state dir holds excerpt text, prompt text, tool-output text or secrets.
  Allowed: counts, sizes, hashes, model families, relative-path indexes.
- R8. Share payloads are built field by field from an allowlist. Never filter a richer object.

**Honesty**
- R9. No user-facing string says "saves", "saved", "reduces cost" or gives a saving figure unless
  the WP is W1-08 and the label is *observed*. Every number shown carries its evidence class:
  *measured*, *observed*, *estimated*.
- R10. Tool output never writes intent, constraint or decision fields.
- R11. Hook paths: no I/O beyond the state append, no blocking calls. Added p99 budget 2 ms.

**Tests**
- R12. Test first for any policy or measurement: the falsifying test lands in the same commit.
- R13. Flag-off must be byte-identical to today's behaviour; add the test when you add a flag.

**Done means** (run chained, stop at the first failure):
```
uv run ruff check . && uv run ruff format --check . && uv run mypy cfxd && \
uv run pytest -q <your test files> && uv run python scripts/check_docs_numbers.py
```
G-FULL (orchestrator, after each merge): the same chain with `uv run pytest -q` (whole suite).

**Commit** only if all pass. One commit per WP, message `WP-ID: <what changed>`. Do not push or
merge.

**Final message** (exactly these headings): Files changed · Tests added · Commands run (each with
its last 3 output lines) · Deviations from the WP · Open questions.

## 7. Review tiers

| Tier | Covers | Review |
|---|---|---|
| 1 | accounting, share serializer, security text, defaults, anything printing a public number, dedupe losslessness | Orchestrator line by line + founder sign-off for security text |
| 2 | CLI wiring, doctor, scripts printing local aggregates | Orchestrator reads the diff and runs the commands |
| 3 | tester guides, README copy (non-evidence) | Skim + copy audit test |

## 8. Worker prompt template

```
Repo: C:/dev/Context-Fabric (github.com/Pranavk098/context-fabric). You are in your own worktree,
branched from w-integration.
Read AGENTS.md, then docs/plans/2026-11-01-plugin-waves-orca.md sections 6 and 7, then your WP below.
Your review tier is <N>. Your file lock: <files>. Nothing else.
<WP section verbatim>
Finish with the done-chain in AGENTS.md. Commit only if it passes. Do not push or merge.
Final message headings: Files changed / Tests added / Commands run / Deviations / Open questions.
```

---

## Wave 0: baseline (orchestrator only, ~1 hour, no workers)

0. Prove the dispatch command works: start one no-op OpenCode worker and one no-op Antigravity
   worker with the section 5 command (prompt: "print the current branch and exit; change nothing").
   Confirm each starts, its branch is readable, and it can be stopped. Fix any flag before step 1;
   do not queue eight builders behind an unproven command.
1. Create `w-integration` from `main`. Run G-FULL. Record the result in the wave report. If red,
   stop and report; no wave starts on a red base.
2. Write `AGENTS.md` (section 6, verbatim) and commit.
3. Copy section 3 (D-1..D-5 as decided and the P4 bar table) into `2026-11-01-wave-decisions.md`
   and commit. The coach threshold from W1-06's sweep is added later as its own dated note. Record the commit hash at the top of the wave report.
4. Build the W1 file-lock table from the WP list below and check that no two WPs share a file.

**Exit:** G-FULL green, `AGENTS.md` and decisions committed, lock table written.

---

## Wave 1: everything that does not depend on anything else (8 builders + 1 researcher, max 5 at once)

Dispatch order: W1-01, W1-02, W1-04, W1-05, W1-09 first (largest or research), then the rest as
slots free.

### W1-01 Share v2 serializer (P2) · OpenCode · tier 1
Files: new `cfxd/share.py`, new `tests/test_share_v2.py`, new `tests/fixtures/leak_repo/` (canary
fixture).
- Implement `build_share(aggregates) -> dict` for schema `cf-share/2` with exactly the fields in
  the technical plan's P2 block, plus `metric: "limit-weighted/1"` (value filled by W1-03 later;
  leave `null` with a TODO marker in the test, not the code).
- `validate_share(payload)` rejects unknown keys, non-numeric values (except `schema`,
  `plugin_version`, `metric`), and booleans other than `consent_aggregate_publish`.
- `render_share_text(payload)` and `render_share_json(payload)` both read only the validated payload.
- `plugin_version` comes from `importlib.metadata.version("context-fabric")` (check the dist name
  in `pyproject.toml`).
- Tests: canary fixture (unique file name, fake secret `cfx_canary_sk_0000`, absolute home path,
  repo name) produces zero canaries in both renders; allowlist property test (add a key to the
  aggregates dict, output unchanged); golden snapshot of both renders.
- Do **not** wire it into `cli.py` (W2-01 does).

### W1-02 Security hardening (P3) · OpenCode code + Antigravity draft text · tier 1
Files: `cfxd/daemon.py` (startup checks only), new `cfxd/content_class.py`, new
`tests/test_content_class.py`, new `tests/test_injection_seeds.py`, `scripts/check_docs_numbers.py`
(add one check), `SECURITY.md` (**draft only**, founder approves).
- Startup: assert token file permissions (POSIX 0600; Windows: ACL limited to current user, warn
  loudly otherwise), assert bind is 127.0.0.1, reject non-local `Host` on authenticated routes.
- `content_class.py`: inventory of every file the daemon writes, each with a declared class;
  `audit_state_dir(path)` fails if any file exceeds its class (e.g. contains newline-separated text
  longer than N chars in a counts-only file). Startup migration deletes any legacy `obs/` blob store.
- Injection seeds: strings planted in tool results never reach a Brief/carry packet; strings planted
  in repo files reach it only inside delimiters, never in intent fields.
- Docs-number check: the carry byte figures in SECURITY.md must equal the constants in code.
- SECURITY.md: Antigravity drafts the P3 outline in a separate commit marked `DRAFT`. The
  orchestrator reviews line by line; the founder approves before W2 merges it.

### W1-03 Limit-weighted metric (P8, compute only) · OpenCode · tier 1
Files: new `cfxd/limit_metric.py`, new `cfxd/data/limit_weights.toml`, new
`tests/test_limit_metric.py`.
- Formula exactly as P8. Weights in the TOML with provenance comments. Plan-tier denominators per
  D-3: official source URL and date, labelled `estimate`; a tier with no official figure has no
  denominator, and `limit_share()` returns `None` for it (test this). Never a made-up default.
- `sensitivity(usage)` returns the headline at w_read = 1/30, 1/67, 1/100.
- Regression anchor test: with all weights = 1 it reproduces the raw totals on
  `tests/golden/` transcripts exactly.
- No CLI or copy changes (W2-01, W2-03).

### W1-04 P4 M1 + M2: repeat-share and Read-stub counts · OpenCode · tier 1
Files: new `scripts/repeat_share.py`, new `tests/test_repeat_share.py`, new synthetic fixtures under
`tests/fixtures/repeat/`.
- Counting definition exactly as P4 M1 (transcript-real, new tokens denominator, three splits:
  by tool, exact vs near-duplicate, within compaction segment). Near-duplicate share and exact share
  are never summed.
- M2: count full re-reads vs `FILE_UNCHANGED`-style stub responses per Read, with bytes.
- Streaming over JSONL; aggregate tables only (R6).
- Script header prints the decisions-file commit hash it was run under (passed as `--bars-commit`;
  refuse to run without it).
- Tests on synthetic transcripts with known answers, including a repeat across a compaction boundary
  (must be excluded) and a usage record repeated per `message.id` (must be counted once).
- **The builder never runs it on real logs.** The orchestrator does, in W2.

### W1-05 P4 M3: restart break-even simulator · OpenCode · tier 1
Files: new `scripts/restart_breakeven.py`, new `tests/test_restart_breakeven.py`.
- Model as P4 M3: per session, measure the starting prefix from that install's logs (no hardcoded
  62K), carry packet size parameter (default 3 KB), cost-to-finish continuing vs restarting at each
  boundary candidate. Boundary candidates are deterministic only: test pass after failures, a
  commit, an explicit user task switch.
- Output: share of 40+ turn sessions with a break-even turn **and positive headroom** (the D-2
  definition), plus the headroom distribution (quartiles). Report "break-even exists" separately
  only as context, never as the bar figure.
- Same `--bars-commit` rule and synthetic tests as W1-04.

### W1-06 Coach instrumentation (P11) · OpenCode · tier 1
Files: `cfxd/coach.py`, new `tests/test_coach_rct.py`, `cfxd/config.py` (only the
`coach_control` default, per D-1), `tests/test_defaults.py` (only that line).
- Flip the `coach_control` default to `True` (D-1) and update its line in `tests/test_defaults.py`.
- Assignment by stable hash of install id, logged once. Test: assignment vs session cost
  correlation ~0 on replay fixtures.
- Log shown / would-have-shown, user action within N turns (restart, end, dismiss, none), cost
  after the decision point. Counts only.
- Offline threshold sweep over fire-turn and cost thresholds in new `scripts/coach_sweep.py`
  (precision and recall per setting). The tester setting is picked from it and frozen in the W0
  decisions file before tester week; no retuning on tester data.

### W1-07 Defaults and golden extension (P12) · OpenCode · tier 1
Files: `tests/test_defaults.py` (except the coach line, which W1-06 owns: merge W1-06 first),
`tests/golden/` (new fixtures), new `tests/test_golden_reports.py`.
- Assert every tester-visible default, including "no retired feature reachable from help, tour,
  guide or default config" (grep `--help` and `docs/testers/` for fold/bound/checkpoint).
- Golden reports: `audit`, `week`, `fidelity`, `handoff-stats` on the golden transcripts reproduce
  exact output. Any diff fails CI.

### W1-08 Diet before/after (observed saving) · OpenCode · tier 1
Files: `cfxd/diet.py`, `cfxd/dietapply.py`, new `tests/test_diet_delta_observed.py`.
- **Revert first.** `cfxd diet --apply` / `--undo` already exist in `dietapply.py` with an apply
  record. Before adding anything, add tests proving: the record is written before any change is
  made; `--undo` restores every disabled item from that record; an interrupted apply is undoable;
  `--undo` with no record says so and changes nothing. The apply confirmation and the tester guide
  print the undo command. If any of these tests fail, fix that first; the before/after work waits.
- On `diet apply`, store the first-turn prefix size (cache-write + new input of turn 1) of the last
  N sessions as a baseline, **keyed by project directory** (stored as a hash, never the path).
  `diet` reports after-apply sessions compared only with the baseline of the same project, with N,
  labelled `observed (this machine, this project, N sessions)`. No cross-project pooling.
- Below 3 sessions after apply in a project: print "not enough sessions yet" and no number.
- This is the one place R9 allows the word "smaller"; never "saved".

### W1-09 Other-CLI log formats (research) · Antigravity · no code
Files: new `docs/research/2026-11-01-cli-log-formats.md` only.
- For Codex CLI, Gemini CLI and OpenCode: where session logs live per OS, file format, whether
  per-message usage (input, cached, output) is recorded, whether tool results are recorded, and
  whether a hook surface exists. Cite official docs or source files with links for every claim.
  Mark anything not confirmed from a primary source as `unverified`.
- Verdict per CLI: `reader feasible` / `reader partial (no usage)` / `not feasible`.

### W1 exit
All merged WPs green under G-FULL; SECURITY.md draft awaiting founder; wave report lists each WP
with branch, commit, check output and review notes.

---

## Wave 2: wire, measure, decide (needs W1 merged)

### W2-01 CLI wiring · OpenCode · tier 2 (sole owner of `cfxd/cli.py`)
Files: `cfxd/cli.py`, `cfxd/week.py`, `cfxd/handoff_stats.py`, `tests/test_cli_wiring.py`.
- Every `--share` path goes through `cfxd/share.py`. `--json` emits the same validated payload.
- `week` and `handoff-stats` lead with limit share (W1-03); raw tokens behind `--verbose`.
- `cfxd brief --paste-safe`: relative paths become `file-N`, no excerpt text.
- `--include-name-digests` replaces the old digest flag and prints the pseudonymous warning.
- Every CLI header prints `plugin_version`.

### W2-02 Doctor depth + structured log (P9) · OpenCode · tier 2
Files: `cfxd/checkup.py` or the doctor module (check which holds `doctor`), `cfxd/daemon.py`
(log line only; W1-02 merged first), new `tests/test_doctor.py`.
- Checks: daemon reachable, token present and permissions, hooks registered, state dir writable and
  content classes (calls W1-02), Claude Code version, Windows launcher fallback. Each prints
  pass/fail and the exact fix command. Output paste-safe.
- Daemon log: one JSON line per hook (hook, duration ms, decision + reason code, bytes in/out,
  segment id), rotation with a hard size cap.

### W2-03 Copy audit and guides (P8, P9) · Antigravity draft, OpenCode test · tier 3
Files: `docs/testers/*.md`, `README.md` (non-evidence sections), new `tests/test_copy_audit.py`.
- Guides lead with limit share; first task is the report from existing logs, second diet, third the
  Brief opt-in. Define "working set", "prefix" and "compaction" on first use.
- The first screen of `docs/testers/README.md` carries the D-1 study disclosure verbatim, and the
  diet guide shows `cfxd diet --undo` next to every apply step.
- Feedback form (W2-04 owns the file; this WP supplies the wording): consent to the coach study and
  consent to publish aggregates are two separate questions.
- Copy audit test fails on banned patterns (R9) in guides and CLI strings.
- README: "Hooks: Claude Code only. Reports: Claude Code" (+ "Codex (beta)" only if W3-04 lands).

### W2-04 P10 intake (schema, validator, aggregation) · OpenCode · tier 1
Files: new `scripts/intake_validate.py`, new `scripts/intake_aggregate.py`, new
`tests/test_intake.py`, `.github/ISSUE_TEMPLATE/tester-feedback.yml` (fields only).
- Validation as P10: schema, version, ranges, duplicate payload hash; quarantine with reason code,
  never repair.
- Consent enforced in the aggregation script. Per-developer median and range, never a pooled mean
  alone. Fewer than 3 developers: script prints "qualitative round only" and no paper table.
- Dry run on 10 synthetic payloads (3 invalid on purpose).

### W2-05 Working-set tracker (P6) · OpenCode · tier 1 · **conditional**
Start only if W2-M shows M1 ≥ 5% **or** M3 clears D-2. Otherwise park. At M1 5–15% (and M3 not
cleared) it is an off-by-default experiment with no tester claim (section 3 bar table).
Files: new `cfxd/working_set.py`, new `tests/test_working_set.py`.
- Pure `apply_event(ws, event) -> ws`; no daemon wiring in this WP. Fields exactly as P6. Constraints
  and decisions only from user or parent messages (R10). Empty is allowed; inferred is a bug.

### W2-M Measurements (orchestrator, on the founder's machine, no workers)
Run W1-04 and W1-05 against `~/.claude/projects` with `--bars-commit <W0 hash>`. Commit the
aggregate tables to `docs/plans/2026-11-xx-p4-results.md` with the verdict per bar, worded as the
technical plan's pre-written outcomes. No bar edits after the run starts.

### W2 exit
G-FULL green; P4 verdicts committed; founder approved SECURITY.md; founder has merged
`w-integration` into `main` and CI on `main` is green.

---

## Wave 3: tag, rehearse, conditional mechanisms

### W3-01 Release `v0.1.1-alpha.2` (founder + orchestrator) · P1
- Release notes schema from P1 (version, commit, ships / opt-in / does not ship, known gaps, install
  command, share-mode statement, "No saving is proven.").
- Founder tags the green `main` commit (never `w-integration`) and pushes the tag. Orchestrator watches `install-smoke` (3 OS), `release`, `ci`, `macos`,
  `replay-check`. Any red: fix forward on a new tag (`alpha.3`); never move a tag.

### W3-02 Cold-install rehearsal (P9) · founder, with orchestrator checklist
Clean user profile on Windows (including `python -m cfxd` fallback) and one of Linux/macOS. Timed,
guide only. Target under 10 minutes to first report. Log every question asked; each is a bug.

### W3-03 Backward dedupe (P5) · OpenCode · tier 1 · **conditional on M1 ≥ 5%**
At M1 5–15% the `dedupe` flag stays off, appears in no guide or README headline, and is enabled only
per tester task as an experiment. Only M1 ≥ 15% allows tester-facing mention, and even then no
saving claim until the paired run.
Files: new `cfxd/dedupe.py`, `cfxd/handlers.py` (PostToolUse branch only), `cfxd/config.py`
(`dedupe` flag, default off), new `tests/test_dedupe_lossless.py`, `tests/test_defaults.py` (one line).
- Algorithm and the three losslessness conditions exactly as P5. Build the Bash/test sub-range path
  first (change 3). Elision receipt `cf-elision/1`.
- Tests: property test (stub + referenced original reconstructs byte for byte), compaction reset,
  small-result pass-through, kill switch byte-identical, latency benchmark under 2 ms added p99.

### W3-04 Codex log reader · OpenCode · tier 1 · **conditional on D-5**
"Reader feasible" with real per-message usage: full reader as below. "Partial (no usage)": counts
only (sessions, turns, tool calls), no cost, limit or token figures, enforced by a test. "Not
feasible": park.
Files: new `cfxd/logs/__init__.py`, `cfxd/logs/claude.py` (move existing transcript iteration
behind the interface, no behaviour change: golden reports must not move), `cfxd/logs/codex.py`,
`tests/golden/codex/`, `tests/test_logs_codex.py`.
- `--source codex` on `audit` and `week` only. Every output carries "Codex (beta)".

### W3-05 Pre-registration for the paired run · Antigravity draft, founder approves
File: `evals/prereg/2026-11-xx-paired-pilot.md`. Task set, arms, the -5 pp non-inferiority
guardrail, stop rule, analysis plan. Committed before any run.
- Budget per D-4: run the 3-task probe per arm first (founder approves the probe spend), then
  ceiling = probe cost per task × tasks × arms × 1.5. Stop at the ceiling and report partial results;
  no silent overrun.

---

## Wave 4: tester week (plugin frozen)

- Founder recruits 3–5 developers (varied OS, long sessions with sub-agents).
- Plugin changes only for the four freeze reasons in the Sprint Plan.
- Orchestrator, 90 minutes a day: triage issues, run `intake_validate.py` on each payload, keep the
  quarantine log.
- End of week: `intake_aggregate.py`, results note answering the four P10 questions, and the P11
  decision rule applied as written.
- After the week: P7 (restart nudge) is built only if M3 cleared **and** the coach treatment arm
  showed a behaviour difference. Otherwise P7 ships as a manual packet command. The paired pilot
  runs only after W3-05 is committed.

---

## Wave report template (orchestrator, one per wave)

```
# Wave <n> report (<date>)
Base: w-integration @ <hash>   Decisions commit: <hash>
| WP | Agent | Branch | Commit | Done-chain (last 3 lines) | Review tier / notes | Status |
Parked WPs and why:
G-FULL after last merge: <last 3 lines>
Founder actions needed:
```
