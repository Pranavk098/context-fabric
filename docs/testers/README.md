# Context Fabric Tester Guides

> **Coach study disclosure (coach is on by default):** You are in a measurement study unless you turn coach off. Half of testers see coach prompts and half do not; which half you are in is fixed by a hash of your install, and the other half only has would-have-shown counts logged (counts and dollars, never text). You can leave the study any time with `cfxd set coach off`.

## Key terms (plain words)
- **Prefix** (starting prefix): the fixed setup sent at the start of a session and re-read on every turn—Claude Code's system prompt and tool definitions, plus your skill listings, MCP connector tools and instructions, memory files, and hook output.
- **Working set**: the state the current session has built from observed tool calls—the files read or edited, recent commands, to-do items, and latest test result.
- **Compaction**: when Claude Code summarizes earlier turns of a long session (`/compact`, manual or automatic) to free context window space, replacing the older raw turns with a summary.

## Guide order (run 1 -> 2 -> 3)
1. **Task 1 — [`checkup.md`](checkup.md) (`cfxd checkup` or `cfxd week` from existing logs):** Run `uv run cfxd checkup` or `uv run cfxd week --share` first from your existing transcripts. `cfxd week` and `cfxd handoff-stats` lead with limit-weighted new tokens (metric `limit-weighted/1`); raw token counts are behind `--verbose`. No plan-tier limit share is shown, because Anthropic publishes no token-denominated 5-hour or weekly limit (decision D-3), so the numbers never imply a percentage of your plan.
2. **Task 2 — [`diet.md`](diet.md) (`cfxd diet`):** Inspect starting-prefix token estimates and unused skills or MCP connectors (`uv run cfxd diet --since 30d --share`). Every apply step has an exact undo right beside it: if you run `uv run cfxd diet --apply` (dry run; undo with `uv run cfxd diet --undo`) or `uv run cfxd diet --apply --yes` (undo with `uv run cfxd diet --undo`), you can restore every disabled plugin at any time. Diet is the only place the word "smaller" may describe a measured change, and only as `observed (this machine, this project, N sessions)`.
3. **Task 3 — [`brief-opt-in.md`](brief-opt-in.md) (Brief opt-in):** Optional, experimental sub-agent handoff Brief test (`uv run cfxd handoff-stats --share`, `uv run cfxd brief --share`, `uv run cfxd brief --paste-safe`). `cfxd brief --paste-safe` replaces relative paths with `file-N` indexes.
- **Compaction reference — [`fidelity.md`](fidelity.md) (`cfxd fidelity`):** Inspect compaction (`/compact`) path retention and dropped edits (`uv run cfxd fidelity --since 30d`; default output is numbers only, with no `--share` flag).

## Sharing, consent, and honesty rules
- **Share format (`cf-share/2`):** `--share` output on `cfxd week --share` is the `cf-share/2` block (numbers and model families only). Name digests exist only behind `--include-name-digests`, which prints a warning and must never be pasted.
- **Report form and two separate consents:** Submit findings via [`.github/ISSUE_TEMPLATE/tester-feedback.yml`](../../.github/ISSUE_TEMPLATE/tester-feedback.yml) (**numbers only, never prompts, paths or commands**). The form asks two separate consent questions: (1) consent to be in the coach measurement study, and (2) consent to publish aggregated counts.
- **Honesty rules:** No saving is proven; every number shown carries its evidence class (*measured*, *observed*, or *estimated*). E-1 found sub-agent re-discovery is a small share of total session cost on the maintainer's machine ("1% to 3%, one machine"), so Brief is an opt-in experimental test and does not claim a saving.
- **Retired features:** `fold`, `bound`, and `checkpoint` are retired and are not part of testing.
