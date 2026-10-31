# External audit packet for Muse (2026-10-31)

**For:** Muse, as an external technical auditor and advisor.
**From:** the founder (and Claude as co-developer).
**Covers:** development from 2026-10-30 to 2026-10-31, the security posture, the
performance we aim for and how we measure it, the plan ahead, and the questions we want
answered.

Repo: github.com/Pranavk098/context-fabric. `main` is at `eef7e03`. Sprint 12 sits on a local
branch, `s12-integration`, which is not pushed yet (8 of 9 packages merged; R-5 pending).
Please be blunt. We would rather hear "this is wrong" now than after testers.

---

## 1. Context since your last audit

- **The v2 registered benchmark was voided.** Two confounds explained it: an MCP
  connection race in the harness (off-arm runs re-announced ~490 connector tools on every
  resumed step), and token usage counted once per transcript line instead of once per
  message. Counting every session also showed the checkpoint "0.44" signal was an
  artifact. Checkpoint actually costs **1.25–1.37×** shipped. The deviation entry is in
  `docs/deviations/2026-10-30-v2-mcp-race-and-double-count.md`.
- **We recognised project drift and re-centred on the founding thesis**: waste in the
  handoff from a parent agent to its sub-agents. The v2 benchmark had never spawned a
  sub-agent.
- **We added cheap gates before paid runs.** The v4 pilot ($1.39), Gate 1 (free:
  re-discovery measured over real logs) and Gate 3 (prior-art research) changed the
  plan before any big spend.

## 2. What was built (2026-10-30 → 2026-10-31)

### 2.1 Benchmark and measurement integrity
| Item | What it does | Evidence it works |
|---|---|---|
| MCP isolation (A1) | `--strict-mcp-config` + empty config for every arm; a run with tool-list churn is an infra failure (`mcp_churn`) | unit tests; v4 pilot had 0 churn |
| Usage dedup (A2) | counts usage once per `message.id` across audit, diet, replay, routing, waste and the benchmark | regression test: 27,630 → 9,483 on a real step |
| Recall fix + guards (A3) | seeded recall facts, `no_restore` control, empty-save = failure, per-session billing | tests |
| v2 dedup re-analysis (A4) | recomputed v2 from raw transcripts, labelled exploratory | `evals/reports/v2_registered_dedup/` |
| macOS CI (C1) | test suite + plugin install + `cfxd doctor` + fixture audit on `macos-latest` | green on main |
| Decisions (D1) | ADR-0010 (the plugin is the product), B8 parked | docs |

### 2.2 Brief v1: the core product feature
| Item | What it does |
|---|---|
| ADR-0011 (B0) | Packet design, per-model budgets (Haiku 6 KB, Sonnet 12 KB, Opus 20 KB), intent-source rule, carry-back shape |
| Selection (B1) | ranks the parent's known files by relevance to the child prompt (paths/symbols, edits, import neighbours, recency), within the model budget |
| Rendering (B2) | packet v0 JSON + text block; excerpts read from disk at brief time and hash-checked; sanitised |
| Intent (B3) | goal/constraints/decisions/tried taken only from user and parent messages and the delegation prompt, never tool output |
| Efficiency (B4) | relative paths, no duplicated goal; more excerpt bytes fit in the same budget |
| Ordering fix | early user facts were pushed out by later turns (Haiku lost all 3 after 5 turns); now reserves a share for opening turns and ranks by prompt overlap; offline check `scripts/check_brief_ordering.py` |
| Policy (R-2) | skips Haiku children and parents that know fewer than 3 files (where Gate 1 shows ~0 to save) |
| Receipts (R1) + `cfxd brief` (U1) | joins each brief to the child transcript: what was sent, what the child re-read anyway |
| Cross-agent demo (X1) | Claude → packet → Codex/OpenCode via AGENTS.md (pointers only, by design) |

### 2.3 v4 delegation benchmark (built, not registered)
- Fixture: `click 8.5.0` vendored, 8 tasks with hidden tests and **planted facts** (constraints
  that exist only in the parent session).
- Arms: none / brief_v0 / brief_v1 / fork / control.
- Pilot findings: the control scored 0/3 on planted facts, so the check detects a missing
  handoff. But the parent copies explicit facts into its delegation prompt, so the arms
  can't be told apart (0.833 each). The fork child always runs on the parent's model, so
  the fork comparison was dropped.
- A design note proposes condition D (4-turn session, facts planted early) and moving the
  primary outcome to child cost. **On hold**, along with all paid runs.

### 2.4 Offline evaluation tools (Sprint 12, no model spend)
| Tool | Result on the founder's real logs |
|---|---|
| Gate 1 re-discovery | 6.6–11.6% of child cost (424 children, $300.80); Opus 14–25%, Sonnet 4–7%, Haiku <1% |
| Brief replay scorer (R-1) | coverage 92.2% of redundant reads at ~2.2 KB; **precision 11.8%**; avoidable 6.4–11.3% of child cost |
| Carry v2 + fidelity scorer (R-4) | restores 46.9% of dropped edited files vs 33.1% for v1 (1.4×; bar was 2×); 30 compactions |
| Bound/coach replay (R-6) | bound: 0 of 11,200 Bash results eligible → retired. Coach: precision 100%, recall 35% (17 of 48 top-cost sessions), always fires at turn 30 |
| Routing by child model (R-9) | sub-agent cost and re-discovery share by family, price only |
| MCP churn rule (R-7) | 3,103 tool/instruction re-announcements across 278 sessions, ~4.4M tokens (dollar split not yet checked) |
| Carry-back research (R-3) | `SubagentStop` with `agent_transcript_path`; a two-hook design proposed; mostly unverified |
| ADR-0012 (R-8) | retires fold; checkpoint-restart superseded by carry v2 |

## 3. Feature scorecard: what we claim and what we don't

| Feature | Intended effect | Status | Next bar |
|---|---|---|---|
| audit / diet / waste / week / ledger / fidelity / refetch | show where context goes | **works** (one machine) | logs from ≥3 developers |
| Brief v1 (+ policy) | cut re-discovery in expensive children | **unproven**; mechanism covers the waste; precision low | precision up offline, then a paid Gate 2 on Opus/Sonnet children |
| Carry v2 | keep more of the working set across `/compact` | better than v1, missed its bar | gate behind a flag; improve |
| Coach | warn on long, expensive sessions | accurate, low recall | threshold sweep |
| Diet `--apply` | remove unused skills/connectors from every turn | deterministic token saving; dollars pending (R-5) | report the estimate |
| MCP churn rule | flag connector re-announcement waste | new | verify the dollar split |
| Checkpoint restart, fold, bound | — | **retired / disproven** | none |
| Carry-back | child summary to parent | broken (async children) | parked with design |

**We make no public saving claim.** The README and the tester guide say "no saving is
proven".

## 4. Security posture (please audit this)

What we have:
- Local daemon on `127.0.0.1:47831`, per-install token (0600; user-profile ACL on
  Windows), `X-Cfx-Token` on every hook, unauthenticated `/health` limited by Host/Origin.
- Logs hold counts, sizes and hashes only. `--share` modes are **pseudonymous, not
  anonymous** (short digest of names).
- Secret masking is best-effort pattern matching, and its limits are documented.
- Brief and carry treat injected context as data: delimiters, a "reference, not
  instructions" prologue, sanitising, byte caps, and **no tool output in intent**. Tests
  seed injection strings into tool results and assert they never reach a packet.
- Offline scripts that read `~/.claude/projects` print aggregates only.
- CI: ruff, format, mypy, tests on Linux/Windows/macOS, a docs-numbers check, and a key
  scan before commit. `pip-audit` was clean on 2026-10-29.

Known gaps we found while preparing this packet:
1. **SECURITY.md is stale.** It says the carry block is 3,000 bytes; carry v2 uses 4 KB and
   now includes user constraints and line ranges.
2. **Brief v1 excerpts are file content injected into a child prompt.** That's a new
   injection surface (a malicious repo file reaches a sub-agent as "data"). SECURITY.md
   doesn't describe it.
3. Brief receipts and `cfxd brief` output contain relative paths. The guide tells testers
   not to paste them; there is no paste-safe mode.
4. Carry v2 changes live compaction behaviour without a flag (being fixed).
5. Windows Application Control can block the `cfxd.exe` launcher; the guide fallback is
   `python -m cfxd`.

## 5. Where we're heading

- **Next 2 days:** tester-ready polish entirely on cheap builders (OpenCode Flash /
  Gemini), with Sonnet only overseeing. See `docs/plans/2026-10-31-delivery-status.md`.
- **Tester week (~1 week out):** testers run Brief, coach, diet and fidelity on real work
  and share counts-only reports. We collect Gate 1 / R-1 style aggregates from their logs.
- **After testers:** Gate 2 (paid, small) on Opus/Sonnet children; then the registered
  delegation benchmark (condition D); then the measurement paper (evidence dump in
  `docs/paper/EVIDENCE-DUMP-2026-10-31.md`).
- **Positioning hypothesis:** "Big models brief sub-agents badly; Context Fabric fixes the
  handoff". The data now says the waste sits in *expensive* children, not small ones.

## 6. Questions for Muse

### Strategy and thesis
1. Gate 1 puts sub-agent re-discovery at 6.6–11.6% of child cost overall and 14–25% for
   Opus children. Is a ceiling that size enough to build a product and a paper around?
   Or should the thesis move toward measurement (fidelity + re-discovery + MCP churn)
   with Brief as a secondary feature?
2. Our data inverts the original framing ("small models briefed badly" became "expensive
   children re-discover"). Is that a stronger or weaker story for early-stage AI
   startups?
3. Is there a bigger lever we're missing? For example, model-tier routing (if a brief lets
   a Sonnet child do an Opus child's job), or MCP/skill diet as the headline saving?
4. Which one result would most change how a startup or a reviewer sees us within a week
   of testing?

### Method and validity
5. R-1 coverage (92%) is partly true by construction; precision is 11.8%. What is the right
   offline proxy for "the child would have used the packet"? Is precision the right
   metric, or should we weight by bytes or tokens of avoided reads?
6. Planted facts didn't separate the arms because parents relay explicit facts. Is
   condition D (facts planted 4 turns before delegation) a fair test, or does it tilt the
   design toward Brief? What would a sceptical reviewer accept?
7. Is it sound to use an offline scorer as a go/no-go gate before paid runs, or does that
   risk tuning on the same logs we'll later report?
8. All data is one developer's logs. What's the minimum (developers, sessions, children)
   for a credible measurement paper? Is counts-only sharing enough, or do reviewers need
   raw-derived metrics we can't share?
9. Carry v2 missed its 2× bar at 1.4×, partly from a bigger byte cap. Should bars be stated
   per byte (efficiency) rather than as absolute restores?
10. Coach: precision 100% and recall 35%. Which trade-off do you prefer for a user-facing
    nudge, and should we measure whether the nudge changes behaviour at all?

### Security
11. Is our injection model for Brief excerpts and carry acceptable for an alpha (data
    delimiters, sanitising, caps, no tool output in intent)? What would you add: an
    excerpt allowlist, skipping files outside git, a per-repo opt-in?
12. Is the per-install token + localhost daemon design sound on Windows without 0600?
    Should the daemon move to a named pipe or Unix socket?
13. `--share` is pseudonymous by name digest. Is that acceptable for testers posting in
    public issues, or should sharing be strictly numeric?
14. Does anything in the receipts or state directory deserve encryption at rest?

### Product and testers
15. For a tester's first 10 minutes, which should we lead with: `fidelity`, `diet`,
    `brief` receipts or the MCP churn warning?
16. Brief v1 ships off by default. Should testers get it on by default (more data) or off
    (safer, honest)?
17. What should the tester feedback form ask so the answers feed the paper (n ≥ 3
    developers) as well as the product?
18. Which features would you cut before the tester build to reduce surface area?

### Process
19. Most code now comes from cheap builders (OpenCode DeepSeek Flash / Gemini) with Sonnet
    reviewing. What review or test practices should we add to keep quality up with weaker
    builders? (Today: file locks, chained checks, mypy, coordinator re-runs every check.)
20. What are we not measuring that you'd expect to see in a project like this?

## 7. What we'd like back

1. The top 5 risks (technical, security, strategic), ranked.
2. Answers to the questions above, or "wrong question, ask this instead".
3. Any experiment or tool you'd run that we can build cheaply offline. We'll treat it as
   a reference point and add it to the next Orca run.
4. A verdict on readiness for testers in ~1 week: what must be true first.
