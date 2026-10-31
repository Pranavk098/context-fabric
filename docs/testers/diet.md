# Tester guide: `cfxd diet`

## What it does
`cfxd diet` reads your local Claude Code transcripts and lists the starting-prefix items (skills, plugins, MCP connectors, memory files, and hooks) present at the start of each session. It shows which skills and connectors were never called in the time window and estimates the tokens they add to every turn.

## Commands to run
```bash
uv run cfxd diet --since 30d
uv run cfxd diet --since 30d --share
uv run cfxd diet --apply
uv run cfxd diet --apply --yes
uv run cfxd diet --undo
```

## What the output means
- **First-turn context and itemized prefix:** median tokens on turn 1, and the estimated tokens coming from your skills, connectors, memory, and hooks.
- **Top items / Never used / Used:** median byte and token size per item, grouped by plugin and connector, split by whether any session in the window called them.
- **Estimated effect:** arithmetic estimate of tokens per turn and list-price API dollars from never-used items. No saving is proven; this is an estimate from listing sizes, not a measured billing drop.

## What to check by hand
1. Run `uv run cfxd diet --since 30d` (without `--share`) on your own screen and check whether any item under "Never used in this window" is something you actually used in the last 30 days.
2. Run `uv run cfxd diet --apply` (dry run) to see which fully-unused local plugins would be disabled.
3. If you run `uv run cfxd diet --apply --yes`, open a new Claude Code session, run `/context`, and compare the starting token count before and after; run `uv run cfxd diet --undo` to re-enable them.

## What to send back
- Send only the numeric output of `uv run cfxd diet --since 30d --share` (and `uv run cfxd week --share` if reporting weekly).
- Numbers only: never pass `--share-digests` when sharing, and never paste the default `cfxd diet` output, prompts, file paths, skill names, or commands.
- Report as plain counts whether any never-used item was a false positive, and the before/after `/context` token counts if you tested `--apply --yes`.

## Known limits
- **No saving is proven.** All token and dollar figures are arithmetic estimates from your setup listings; Brief is opt-in and experimental with no proven saving.
- **Account-synced plugins cannot be disabled locally** by `diet --apply`; only local plugins can be toggled automatically.
- **Platform status:** Windows needs Git for Windows for plugin hooks; macOS is not verified by the founder.
- **One-command `cfxd checkup`:** not available in this build.
