# Hook spike: what Claude Code lets a plugin do (2026-10-21)

Sprint 1, Day 1. Question: can Context Fabric fold, brief and carry from inside Claude Code using hooks
only, with no proxy? Method: a scratch project with hooks pointing at a logging test server
(`spikes/claude-code-hooks/`), driven by headless `claude -p` sessions (Claude Code 2.1.307, Windows 11).
Docs read: https://code.claude.com/docs/en/hooks. Evidence level: *exploratory* (single runs, headless mode).

## Results

| # | Test | Hook used | Result |
|---|---|---|---|
| 1 | Rewrite a sub-agent's prompt | `PreToolUse` on tool `Agent`, `permissionDecision: allow` + `updatedInput` | **Works.** The sub-agent's reply began with the marker the hook added (`CFX-REWRITE-OK`) |
| 2 | Inject context into a sub-agent at spawn | `SubagentStart` → `additionalContext` | **Works.** The sub-agent reported the codeword it was only given by the hook (`PAPAYA-42`) |
| 3 | Replace a tool's output (fold) | `PostToolUse` on `Read` → `updatedToolOutput` | **Works.** The model saw only the stub (`FOLD-STUB-OK`), never the 200-line file |
| 4 | Add context at session start | `SessionStart` → `additionalContext` | **Works as a `command` hook. The `http` form never fired** (no request reached the server) |
| 5 | Survive compaction | `PreCompact` + `SessionStart` (`source: compact`) | **Works.** `/compact` fired `PreCompact` (`trigger: manual`), then `SessionStart` with `source: compact`; the codeword was still known afterwards |
| 6 | Fail open | All `http` hooks, server stopped | **Works.** A session using `Read` and `Agent` completed normally in 8 s with no errors |
| 7 | Hook latency (server side) | `http` | 0.4–1.1 ms per event in the test server. End-to-end client overhead not yet measured (`cfxd doctor` will) |

## Facts the plugin depends on

- **Sub-agent tool** is `Agent`; `tool_input` keys seen: `description`, `prompt`, `subagent_type`, `run_in_background`
  (docs also list `model`). `updatedInput` **replaces the whole object**, so echo every field back.
- **`SubagentStart` payload has no task prompt** (`agent_id`, `agent_type`, `cwd`, `hook_event_name`, `prompt_id`,
  `session_id`, `transcript_path`). To brief on the task we must read the prompt at `PreToolUse:Agent`.
- **`SubagentStop`** carries `last_assistant_message` and `agent_transcript_path`: enough for a hand-back receipt.
- **`Read` output shape** for `updatedToolOutput`: `{"type": ..., "file": {"filePath", "content", "numLines",
  "startLine", "totalLines"}}`. Per the docs, a value that doesn't match a built-in tool's schema is **silently
  ignored**, so every folded tool needs a shape test. Other `tool_response` shapes captured (keys only):

  | Tool | Keys | Field to fold |
  |---|---|---|
  | Bash | `stdout`, `stderr`, `interrupted`, `isImage`, `noOutputExpected` | `stdout` (and long `stderr`) |
  | Grep | `mode`, `numFiles`, `filenames`, `content`, `numLines`, `totalLines` | `content` / `filenames` |
  | Glob | `filenames`, `durationMs`, `numFiles`, `truncated`, `totalMatches`, `countIsComplete` | `filenames` |
  | WebFetch | `bytes`, `code`, `codeText`, `result`, `durationMs`, `url` | `result` |
- `PostToolUse` runs **concurrently** for parallel tool calls: the daemon must be multi-threaded.
- Docs: telemetry (OTel tool spans) records the original output before the hook runs, so folds don't hide
  data from the user's own observability.

## What changes in the Sprint 1 plan

1. **No shim on the hot path for Claude Code.** `http` hooks POST straight to the daemon, so there is no process
   start per tool call. The command shim is only for `SessionStart`/`PreCompact` (rare, latency doesn't matter)
   and for clients without HTTP hooks (OpenCode, Codex: see `docs/research/HOOKS-OPENCODE-CODEX.md`).
2. **Fold (Fri) gets simpler:** use `PostToolUse` + `updatedToolOutput` instead of redirecting tools to MCP
   folding tools in `PreToolUse`. Expansion stays an MCP tool (`cfx expand` / `ctx://obs/<id>`).
   The stub must say plainly what it is and how to expand: in test 3 the model called the bare stub
   "a placeholder" and was unsure what to do with it.
3. **Brief (Sat):** compile the packet at `PreToolUse:Agent` (that's where the task prompt is) and deliver it
   by appending to `prompt` in `updatedInput`. `SubagentStart.additionalContext` is the cleaner channel (a system
   reminder, not part of the visible prompt) but has no prompt and no reliable link to *which* `Agent` call
   spawned it when several run in parallel; revisit once we can correlate them.
4. **Carry (Sun):** `PreCompact` snapshots state, `SessionStart` with `source: compact` re-injects it. Both as
   command hooks via the shim.

## Not yet tested

- Interactive (TUI) sessions: everything above ran under `claude -p`.
- Auto-compaction (`trigger: auto`); only manual `/compact` was exercised.
- That `updatedToolOutput` is accepted for Bash, Grep, Glob, WebFetch (shapes captured above, replacement only
  tested on Read).
- Two hooks returning `updatedInput` for the same call (e.g. alongside another plugin): precedence is
  undocumented. Matters for coexistence with context-mode.
- Why `SessionStart` ignores `http` hooks (by design or a bug); the docs page was not conclusive.
