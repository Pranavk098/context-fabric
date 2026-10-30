# Ten-day plan (2026-10-30 → 2026-11-08)

> **Superseded 2026-10-30** by `2026-10-30-brief-first-plan.md` (Brief-first). Kept for history; Wave 1 here is done.


## 0. The v2 confound: found

Evidence: paired long-checkout rep 0 transcripts (off `7e5be006…`, shipped `1d4c469f…`).

**Cause 1: an MCP connection race in the harness (not the plugin).**
Each step is a fresh `claude -p --resume`. The account's ~490 connector tools are not
blocked by `--setting-sources project,local`. In off, the first turn starts before they
finish connecting, so on every step Claude Code announces 14 tools, then adds 492, then
removes them on the next resume and adds them back again:

| | off | shipped |
|---|---|---|
| `deferred_tools_delta` attachments | 27 (~827 KB) | 1 (~49 KB) |
| `mcp_instructions_delta` attachments | 26 (~170 KB) | 1 (~14 KB) |

Shipped's SessionStart hook (timeout up to 10s) delays the first turn long enough for
every server to connect, so the list is announced once. That accounts for the ~30K cache
rewrite per step and the 83K vs 49K context. It also explains why short (single-step)
tasks show no gap. The plugin "saved" tokens only by losing a race.

**Cause 2: a usage double-count in the analyzer (affects every arm).**
Claude Code writes one transcript line per content block, and each line repeats the
message's `usage`. The step totals sum lines, not messages. Off "tax" step: reported
27,630 cache writes = 1035×2 + 5704×4 + 2744. Deduplicating by `message.id` cuts that
run's weighted tokens from 1.64M to 0.58M (off) and from 0.81M to 0.33M (shipped). The
inflation differs by arm, so the ratios move too.

**Fixes before v3**
1. Isolate MCP: `--strict-mcp-config --mcp-config <empty.json>` and turn off account
   connectors for benchmark runs. Assert that the transcript contains exactly one
   `deferred_tools_delta`, or mark the run as an infra failure.
2. Deduplicate usage by `message.id` in the runner/analyzer, add a regression test using
   the tax-step numbers above, and re-analyze v2 with dedup (exploratory, labelled).
3. Expect off ≈ shipped after the fix. If it isn't, there is a second confound.
4. Recall test: synthetic planted facts plus a no-restore control that must score ~0.
5. A 0-byte save is a hard failure.
6. Registration: checkpoint vs shipped is primary; add a second fixture.

**Side finding worth a post (after replication):** headless `claude -p --resume` with
many MCP connectors re-sends roughly 30K tokens of tool listings per invocation. That's a
real, measurable waste pattern, and it fits naturally as a waste-detector rule.

## 1. Where we fall behind: redemption list

Muse's six, in order, with my additions marked ★.

1. **Paired benchmark → a real number.** Fix the two confounds above and run v3. Nothing
   public until it exists.
2. **External log donors** via `week --share` (counts only). This fixes n=1 for both the
   paper and credibility.
3. **Measurement paper draft now**, from existing data: compaction fidelity (21% of held
   files named; 460 of 614 edited files dropped; 87 of 841 paths invented), the refetch
   null, harness methods. v3 becomes the results section.
4. **OpenCode/Codex adapters** (log readers exist; plugins don't).
5. **Park B8** with a written note. It isn't on the critical path.
6. **Two ADRs**: compiler vs plugin; ContextDB in or out.
7. ★ **macOS without a Mac**: GitHub Actions `macos-latest` (free on public repos) runs
   the test suite plus a scripted install and `cfxd doctor` smoke test on every push.
   That replaces the missing MacBook for everything except interactive UX.
8. ★ **Release pipeline**: tagged builds, a one-line install, `cfxd doctor` as the first
   command. Target: install to first report in under 5 minutes.
9. ★ **Benchmark hygiene as product**: the MCP isolation and dedup checks become asserts
   in the harness, so nobody (including reviewers) can hit this again. Publish the
   harness so anyone can reproduce it.
10. ★ **One-sentence contribution**: "Production compaction forgets most of what the agent
    was working on, and a checkpoint/restore layer recovers it at X% lower cost." Keep X
    empty until v3 fills it.

## 2. Features to ship before testers (quality bar: tested, documented, doctor-checked)

Ranked by impact on what testers can see, given what exists.

| # | Feature | Why it attracts | Status → target |
|---|---|---|---|
| F1 | **Compaction fidelity check** (`cfx fidelity`, "what did your /compact forget?") on the user's own logs | Instant, personal, shareable result; the paper's headline finding | metric exists → polished command + report |
| F2 | **Session report card** (HTML): where tokens went, waste callouts, the cost of each pattern | The screenshot people post | diet/waste/ledger exist → one rendered page |
| F3 | **Waste-detector rule pack**, including MCP tool-list churn, repeated reads, oversized tool outputs | Concrete "you lost $N here" lines | waste exists → add churn rule from §0 |
| F4 | **Checkpoint as the hero feature**: reliable save (0-byte = error), restore, one command | The only intervention with a promising signal (~0.44 exploratory) | experimental → hardened, guarded |
| F5 | **Cross-agent handoff** (Claude → Codex/OpenCode via packet) | Novel; portability story; the packet spec as moat | offline tool → one documented demo flow |
| F6 | **Reproducible public bench** (`make bench` with a fixture) | Startups trust what they can run | harness → isolated, documented |
| F7 | **Tester kit**: TESTING-GUIDE, `--share` export, a feedback template | Turns testers into data donors | partial → complete |

Explicitly not before testers: the live router, ContextDB, B8, new metrics.

## 3. Day-by-day

| Day | Build | Verify |
|---|---|---|
| 1 | Deviation entry (repo + private); MCP isolation + infra assert; usage dedup + test | re-analyze v2 with dedup; small off/shipped probe shows ≈1 |
| 2 | Recall redesign (planted facts, no-restore control); 0-byte guard; second fixture; register v3 (SHA) | no-restore control scores ~0 on a pilot |
| 3 | Start v3 overnight. macOS CI job | CI green on macos-latest |
| 4 | F4 checkpoint hardening; F3 churn rule | tests; rule fires on the v2 off transcripts |
| 5 | F1 fidelity command + report | runs on my logs and on a fresh install |
| 6 | F2 report card | renders in light and dark; numbers match the CLI |
| 7 | v3 analysis (as registered). Paper: methods + fidelity sections | docs-numbers checker covers new figures |
| 8 | F5 handoff demo; F7 tester kit; release pipeline + install script | clean-machine install < 5 min (Windows + macOS CI) |
| 9 | ADRs; park-B8 note; README rewrite around one sentence, only proven claims | docs-numbers passes |
| 10 | Buffer / fixes. Draft the post (MCP churn finding + fidelity chart); publish only what v3 and replication support | — |

Day 10 hand-off to testers: install, run `cfxd doctor`, use normally for a week, run
`fidelity` + report card, `week --share`.

## 4. Traction moves (post-testers)

- One post: "What /compact forgets", with the fidelity chart and a run-it-yourself command.
  Second post: the MCP churn waste (after one more machine reproduces it).
- Packet spec on its own page, with a small conformance test, so other tools can emit it.
- List on directories where Claude Code / Codex users look; reply in threads about
  context cost with the free fidelity check, not a pitch.
- After v3: a short arXiv preprint (measurement paper), then the systems paper once
  external logs and a competitor baseline exist.
