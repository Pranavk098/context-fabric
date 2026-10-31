# Tester guide: `cfxd checkup`

## Run this first
`cfxd checkup` is the single command every tester runs first. It prints aggregate counts and percentages only (there is no `--share` flag because the default output is already share-safe) and ends with the footer line `safe to paste: counts and percentages only`.

```bash
uv run cfxd checkup
uv run cfxd checkup --since 7d
uv run cfxd checkup --since all
```

## Sections in order and what they mean
1. **Diet (`Diet (sessions: N; an estimate, not a bill)`):** counts of skills and MCP connectors never called in the window, plus estimated setup-prefix tokens and unused-prefix tokens per turn. This is an arithmetic size estimate, not a measured billing drop.
2. **Fidelity (`Fidelity (frozen metrics v1, compactions: N)`):** `/compact` boundary count and three rates:
   - `M1 unsupported path rate`: share of path mentions in `/compact` summaries never seen earlier in tool inputs or results.
   - `M2 dropped edit rate`: share of files edited before `/compact` that the summary did not name.
   - `M3 unsupported pass claims`: `/compact` summaries claiming tests passed when the last observed test command failed or none ran.
3. **Sub-agents (`Sub-agents (cfxd handoff-stats numbers)`):** whether Brief is on, sessions with child transcripts, joined child count, per-model-family rows (`haiku`, `sonnet`, `opus`, `other`) showing child count, children whose parent knew 3+ files, re-reads of those files out of total read calls, re-discovery share, and the estimated `re-discovery cost share of total session cost` (`lower` to `upper`).
4. **Footer:** ends with `safe to paste: counts and percentages only`.

## What to paste into the Tester report issue form
Open [`.github/ISSUE_TEMPLATE/tester-feedback.yml`](../../.github/ISSUE_TEMPLATE/tester-feedback.yml) and fill in:
- **OS and version** and **Claude Code / cfxd version** (`claude --version` and the first line of `uv run cfxd week --share`).
- **Sessions by model (counts):** model session counts from `uv run cfxd week --share`.
- **`cfxd checkup --share (or week --share) output`:** paste the entire output block of `uv run cfxd checkup` (from `cfxd checkup (numbers only, ...)` down through `safe to paste: counts and percentages only`), plus `uv run cfxd week --share` if available.
- **Install minutes, friction log, Likert ratings (Diet, Fidelity, Brief), and checkboxes:** numbers only—never paste prompts, file paths, skill names, or commands.

## Honesty and scope notes
- **No saving is proven.** All token and cost figures are estimates; Brief is opt-in and experimental.
- **Brief is a test, not a promise:** E-1 found re-discovery is a small share of total session cost on the maintainer's machine ("1% to 3%, one machine").
- **Retired features:** `fold`, `bound`, and `checkpoint` are retired and are not part of testing.
