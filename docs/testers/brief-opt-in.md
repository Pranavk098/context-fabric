# Tester guide: Brief opt-in (`cfxd brief`)

## What it does
Brief is an opt-in, experimental hook feature that adds a short note of parent-touched files, constraints, decisions, and short file excerpts when Claude Code spawns a sub-agent. `cfxd brief` joins those local hook receipts with your session transcripts to show packet sizes, target models, and how many briefed files the sub-agent re-read anyway.

## Commands to run
```bash
uv run cfxd checkup
uv run cfxd handoff-stats --share
uv run cfxd doctor
uv run cfxd set brief_v1 on
uv run cfxd brief
uv run cfxd brief --session SESSION
uv run cfxd brief --share
uv run cfxd set brief_v1 off
uv run cfxd week --share
uv run cfxd ledger --days 30 --share
```

## Structured task: Brief on vs. off
1. **Run 1 (Brief on):** Run `uv run cfxd set brief_v1 on`. Pick a task in your repo that spawns sub-agents (for example, exploring two modules and fixing a test). When done, run `uv run cfxd brief --session SESSION` and `uv run cfxd brief --share`, and note the handoff count, packet bytes, `re-read of briefed`, `parent-read left out`, `child tokens`, and `child turns`.
2. **Run 2 (Brief off):** Run `uv run cfxd set brief_v1 off`. Pick a similar sub-agent task in the same repo with the same model tier. When done, run `uv run cfxd brief --session SESSION` and `uv run cfxd brief --share`, and note the same counts side by side.

## What the output means
- **`decision` and `target model`:** whether the brief was injected or skipped (with skip reason), and the sub-agent model family (`haiku`, `sonnet`, `opus`, or `unknown`).
- **`packet`:** byte size, model budget percentage, and item count sent to the sub-agent.
- **`re-read anyway` and `left out, re-read`:** how many briefed files the child opened again (a signal, not a score), and how many parent-read files omitted from the brief the child opened.
- **`child tokens`, `compactions`, `outcome`, `unmatched`:** child token breakdown, turn count, overflows, and any transcript-matching failure reason.

## What to check by hand
1. Run `uv run cfxd brief` on your own screen (this view prints relative file paths) and check whether the `top items and why` ranking matched the sub-agent's task.
2. Check whether the sub-agent behaved worse, ignored the brief, or still re-read the files listed under `re-read anyway`.

## What to send back
- Send the output of `uv run cfxd checkup`, `uv run cfxd handoff-stats --share`, `uv run cfxd brief --share`, `uv run cfxd week --share`, and `uv run cfxd ledger --days 30 --share`, plus your Run 1 (Brief on) vs. Run 2 (Brief off) counts.
- Numbers only: never pass `--share-digests`, and never paste the default `cfxd brief` output (which prints relative paths), prompts, file paths, or commands.

## Known limits
- **Opt-in and experimental; no saving is proven.** Brief v1 is off by default and has no proven token or cost saving. E-1 found re-discovery is a small share of total session cost on the maintainer's machine ("1% to 3%, one machine"), so Brief is a test, not a promise.
- **Platform status:** Windows needs Git for Windows (hook startup runs in Git Bash); macOS is not verified by the founder.
- **Sub-agent attribution and non-Claude children:** carry-back is unverified and off; Codex and OpenCode children receive pointers only.
- **Retired features and unavailable flags:** `fold`, `bound`, and `checkpoint` are retired and are not part of testing; `cfxd set brief_excerpts` is not available in this build (use `uv run cfxd checkup` and `uv run cfxd handoff-stats --share` for handoff summary numbers).
