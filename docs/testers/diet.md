# Tester guide (Task 2): `cfxd diet`

## What it does
`cfxd diet` is your second task after running `cfxd checkup` or `cfxd week --share`. It reads your local Claude Code transcripts and lists the starting **prefix** items—the fixed setup sent at the start of a session and re-read on every turn (Claude Code's system prompt and tool definitions, plus your skills, plugins, MCP connectors, memory files, and hooks)—and compares them against the tools called in the session's **working set** (the files read or edited, commands, to-dos, and test state observed across turns, before or after any **compaction** where `/compact` summarizes earlier turns). It shows which skills and connectors were never called in the time window and estimates the tokens they add to every turn.

## Commands to run (with `--undo` beside every `--apply` step)
```bash
uv run cfxd checkup
uv run cfxd diet --since 30d
uv run cfxd diet --since 30d --share
uv run cfxd diet --apply          # dry run; undo any apply with: uv run cfxd diet --undo
uv run cfxd diet --apply --yes    # disables unused local plugins; undo: uv run cfxd diet --undo
uv run cfxd diet --undo
```

## What the output means
- **First-turn context and itemized prefix:** median tokens on turn 1, and the estimated tokens coming from your skills, connectors, memory, and hooks.
- **Top items / Never used / Used:** median byte and token size per item, grouped by plugin and connector, split by whether any session in the window called them.
- **Estimated effect:** arithmetic estimate of tokens per turn and list-price API dollars from never-used items. No saving is proven by this estimate; it is calculated from listing sizes, not a measured billing drop.
- **Observed before/after (`After apply`):** when you apply changes (`uv run cfxd diet --apply --yes`; undo any time with `uv run cfxd diet --undo`), `diet` stores the median first-turn prefix tokens of your recent sessions per project (keyed by a project directory hash) as a baseline. Once a project has 3 or more new sessions after apply (fewer than 3 prints `not enough sessions yet`), `cfxd diet` compares those new sessions against that same project's baseline only. Diet is the only place the word "smaller" may describe a measured change, and only as `observed (this machine, this project, N sessions)`.

## What to check by hand
1. Run `uv run cfxd diet --since 30d` (without `--share`) on your own screen and check whether any item under "Never used in this window" is something you actually used in the last 30 days.
2. Run `uv run cfxd diet --apply` (dry run; if you apply changes, `uv run cfxd diet --undo` restores them) to see which fully-unused local plugins would be disabled.
3. If you run `uv run cfxd diet --apply --yes` (revert any time with `uv run cfxd diet --undo`), run 3 or more sessions in the same project and re-run `uv run cfxd diet --since 30d --share` to inspect the per-project before/after line labelled `observed (this machine, this project, N sessions)` (which reports how many tokens smaller or larger the first-turn prefix was under `observed (this machine, this project, N sessions)`). Run `uv run cfxd diet --undo` to re-enable every disabled plugin.

## What to send back
- Send only the numeric output of `uv run cfxd diet --since 30d --share` (and `uv run cfxd week --share`, which emits the `cf-share/2` block of numbers and model families only).
- Numbers only: never pass `--include-name-digests` when sharing (it prints a warning that pseudonymous digests are reversible for common names and must never be pasted), and never paste the default `cfxd diet` output, prompts, file paths, skill names, or commands.
- Report as plain counts whether any never-used item was a false positive, and the `observed (this machine, this project, N sessions)` before/after token counts if you tested `uv run cfxd diet --apply --yes` (undo: `uv run cfxd diet --undo`).

## Known limits
- **No saving is proven.** Listing-size token and dollar figures are arithmetic estimates (*estimated*), and before/after first-turn prefix changes are single-project observations labelled `observed (this machine, this project, N sessions)`, not a paired counterfactual; Brief is opt-in and experimental with no proven saving (E-1 found re-discovery is a small share of total session cost on the maintainer's machine: "1% to 3%, one machine", so Brief is a test, not a promise).
- **Account-synced plugins cannot be disabled locally:** `uv run cfxd diet --apply` (undo: `uv run cfxd diet --undo`) toggles only local plugins automatically.
- **Platform status:** Windows needs Git for Windows for plugin hooks; macOS is not verified by the founder.
- **One-command summary and retired features:** run `uv run cfxd checkup` for the combined numbers-only summary; `fold`, `bound`, and `checkpoint` are retired and are not part of testing.
