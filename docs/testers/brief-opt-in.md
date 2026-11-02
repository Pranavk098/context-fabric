# Tester guide (Task 3): Brief opt-in (`cfxd brief` and `cfxd handoff-stats`)

## What it does
Brief is your third, optional task (after `cfxd checkup` / `cfxd week` and `cfxd diet`). It is an opt-in, experimental hook feature that passes a short note of the parent session's **working set**—the files read or edited, recent commands, to-do items, and latest test state observed so far, plus user-stated constraints and decisions—when Claude Code spawns a sub-agent, instead of leaving the sub-agent with only its starting **prefix** (the fixed system prompt, tool definitions, skills, connectors, memory, and hooks sent on turn 1 and re-read every turn). `cfxd brief` joins local hook receipts with your session transcripts to show packet sizes, target models, **compaction** counts (when `/compact` summarizes earlier turns to free context space), and how many briefed files the sub-agent re-read anyway.

## Commands to run
```bash
uv run cfxd checkup
uv run cfxd handoff-stats --share
uv run cfxd handoff-stats --verbose
uv run cfxd doctor
uv run cfxd set brief_v1 on
uv run cfxd brief
uv run cfxd brief --paste-safe
uv run cfxd brief --session SESSION
uv run cfxd brief --share
uv run cfxd set brief_v1 off
uv run cfxd week --share
uv run cfxd ledger --days 30 --share
```

## Structured task: Brief on vs. off
1. **Run 1 (Brief on):** Run `uv run cfxd set brief_v1 on`. Pick a task in your repo that spawns sub-agents (for example, exploring two modules and fixing a test). When done, run `uv run cfxd brief --session SESSION --paste-safe` and `uv run cfxd brief --share`, and note the handoff count, packet bytes, `re-read of briefed`, `parent-read left out`, `child tokens`, and `child turns`.
2. **Run 2 (Brief off):** Run `uv run cfxd set brief_v1 off`. Pick a similar sub-agent task in the same repo with the same model tier. When done, run `uv run cfxd brief --session SESSION --paste-safe` and `uv run cfxd brief --share`, and note the same counts side by side.

## What the output means
- **`cfxd handoff-stats` leads with limit-weighted new tokens (`limit-weighted/1`):** `cfxd handoff-stats` (and `cfxd week`) leads with limit-weighted new tokens (`estimated` from price-ratio weights in `cfxd/data/limit_weights.toml`). Raw token counts and the `w_read` sensitivity table are behind `--verbose` (`uv run cfxd handoff-stats --verbose`). No plan-tier limit share is shown (`Limit share: n/a`), because Anthropic publishes no official token-denominated 5-hour or weekly limit for any plan tier (decision D-3)—it is not a percentage of your plan.
- **`decision` and `target model` (`cfxd brief`):** whether the brief was injected or skipped (with skip reason), and the sub-agent model family (`haiku`, `sonnet`, `opus`, or `unknown`).
- **`packet`:** byte size, model budget percentage, and item count sent to the sub-agent.
- **`re-read anyway` and `left out, re-read`:** how many briefed files the child opened again (a signal, not a score), and how many parent-read files omitted from the brief the child opened.
- **`--paste-safe` vs. default `cfxd brief`:** `uv run cfxd brief --paste-safe` replaces relative file paths with `file-N` indexes (`file-1`, `file-2`, ...; no excerpt text is stored or printed either way).
- **`child tokens`, `compactions`, `outcome`, `unmatched`:** child token breakdown, turn count, compactions and context overflows, and any transcript-matching failure reason.

## What to check by hand
1. Run `uv run cfxd brief` on your own screen (this view prints relative file paths; use `uv run cfxd brief --paste-safe` to replace paths with `file-N`) and check whether the `top items and why` ranking matched the sub-agent's task.
2. Check whether the sub-agent behaved worse, ignored the brief, or still re-read the files listed under `re-read anyway`.

## What to send back
- Send the output of `uv run cfxd checkup`, `uv run cfxd handoff-stats --share`, `uv run cfxd brief --share`, `uv run cfxd week --share` (the `cf-share/2` block of numbers and model families only), and `uv run cfxd ledger --days 30 --share`, plus your Run 1 (Brief on) vs. Run 2 (Brief off) counts.
- Numbers only: never pass `--include-name-digests` (which prints a warning and must never be pasted), and never paste the default `cfxd brief` output (which prints relative paths; use `--share` or `--paste-safe`), prompts, file paths, or commands.

## Known limits
- **Opt-in and experimental; no saving is proven.** Brief v1 is off by default and no saving is proven. E-1 found re-discovery is a small share of total session cost on the maintainer's machine ("1% to 3%, one machine"), so Brief is a test, not a promise.
- **Coach study is separate and off by default:** turning coach on (`cfxd set coach on`) enters an install-hash measurement study where half of installs see prompts and half do not; leave any time with `cfxd set coach off`.
- **Platform status:** Windows needs Git for Windows (hook startup runs in Git Bash); macOS is not verified by the founder.
- **Sub-agent attribution and non-Claude children:** carry-back is unverified and off; Codex and OpenCode children receive pointers only.
- **Retired features and unavailable flags:** `fold`, `bound`, and `checkpoint` are retired and are not part of testing; `cfxd set brief_excerpts` is not available in this build (use `uv run cfxd checkup` and `uv run cfxd handoff-stats --share` for handoff summary numbers).
