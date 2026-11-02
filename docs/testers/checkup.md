# Tester guide (Task 1): `cfxd checkup` and `cfxd week`

## Run this first from your existing logs
Your first task is running `cfxd checkup` or `cfxd week --share` over your existing local Claude Code transcripts (`~/.claude/projects`). Both work before any new session runs.

- **Prefix** (starting prefix): the fixed setup sent at the start of a session and re-read on every turn—Claude Code's system prompt and tool definitions, plus your skill listings, MCP connector tools and instructions, memory files, and hook output.
- **Compaction**: when Claude Code summarizes earlier turns of a long session (`/compact`, manual or automatic) to free context window space, dropping the older raw turns.
- **Working set**: the state the current session has built from observed tool calls—the files read or edited, recent commands, to-do items, and latest test result.

```bash
uv run cfxd checkup
uv run cfxd checkup --since 7d
uv run cfxd checkup --since all
uv run cfxd week --share
uv run cfxd week --verbose
```

## What `cfxd checkup` prints (in fixed order)
`cfxd checkup` prints aggregate counts and percentages only (there is no `--share` flag because the default output is already share-safe) and ends with `safe to paste: counts and percentages only`.

1. **Diet (`Diet (sessions: N; an estimate, not a bill)`):** counts of skills and MCP connectors never called in the window, plus estimated setup-prefix tokens and unused-prefix tokens per turn (an arithmetic size estimate, not a measured billing drop).
2. **Fidelity (`Fidelity (frozen metrics v1, compactions: N)`):** `/compact` boundary count and three rates comparing compaction summaries against the session's working set:
   - `M1 unsupported path rate`: share of path mentions in `/compact` summaries never seen earlier in tool inputs or results.
   - `M2 dropped edit rate`: share of files edited before `/compact` that the summary did not name.
   - `M3 unsupported pass claims`: `/compact` summaries claiming tests passed when the last observed test command failed or none ran.
3. **Sub-agents (`Sub-agents (cfxd handoff-stats numbers)`):** whether Brief is on, sessions with child transcripts, joined child count, per-model-family rows (`haiku`, `sonnet`, `opus`, `other`) showing child count, children whose parent knew 3+ files, re-reads of those files out of total read calls, re-discovery share, and the estimated `re-discovery cost share of total session cost` (`lower` to `upper`).
4. **Footer:** ends with `safe to paste: counts and percentages only`.

## What `cfxd week` and `cfxd handoff-stats` lead with
- **Limit-weighted new tokens (`limit-weighted/1`) lead:** `cfxd week` and `cfxd handoff-stats` lead with limit-weighted new tokens (`Limit-weighted new tokens: ... (estimated: price-ratio weights from cfxd/data/limit_weights.toml)`).
- **Raw token counts are behind `--verbose`:** pass `uv run cfxd week --verbose` or `uv run cfxd handoff-stats --verbose` to append raw token counts (`input`, `cache write`, `cache read`, `output`) and the `w_read` sensitivity table.
- **No plan-tier limit share is shown (decision D-3):** both commands print `Limit share: n/a` because Anthropic publishes no official token-denominated 5-hour or weekly limit for any plan tier. We state that plainly rather than implying a percentage of your plan.
- **`--share` emits `cf-share/2`:** `uv run cfxd week --share` appends the `cf-share/2` paste-ready block containing numbers and model families only (`opus`, `sonnet`, `haiku`). Name digests exist only behind `--include-name-digests`, which prints a warning and must never be pasted.

## Coach study note
Coach is **off** by default. You are in a measurement study if you turn coach on. Half of testers see coach prompts and half do not; which half you are in is fixed by a hash of your install. You can leave the study any time with `cfxd set coach off`.

## What to paste into the Tester report issue form
Open [`.github/ISSUE_TEMPLATE/tester-feedback.yml`](../../.github/ISSUE_TEMPLATE/tester-feedback.yml) and fill in:
- **OS and version** and **Claude Code / cfxd version** (`claude --version` and the first line of `uv run cfxd week --share`).
- **Sessions by model (counts):** model session counts from `uv run cfxd week --share`.
- **`cfxd checkup --share (or week --share) output`:** paste the entire output block of `uv run cfxd checkup` and the `cf-share/2` block from `uv run cfxd week --share`.
- **Two separate consent questions and the leak checkbox:** answer (1) consent to be in the coach measurement study and (2) consent to publish aggregated counts separately, and check the safe-output box confirming you pasted numbers only—never prompts, file paths, skill names, or commands.

## Honesty and scope notes
- **No saving is proven.** Every token and cost figure carries its evidence class (*measured*, *observed*, or *estimated*); Brief is opt-in and experimental and does not claim a saving.
- **Brief is a test, not a promise:** E-1 found re-discovery is a small share of total session cost on the maintainer's machine ("1% to 3%, one machine").
- **Retired features:** `fold`, `bound`, and `checkpoint` are retired and are not part of testing.
