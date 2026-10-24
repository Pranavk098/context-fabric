# ADR-0008 — Brief and carry are built from observed facts only

- **Status:** Accepted
- **Date:** 2026-10-24
- **Evidence:** `tests/test_cfxd_brief.py`, `test_cfxd_carry.py`, `test_cfxd_session_state.py`; live runs on Windows with
  `claude -p` and the plugin loaded: a sub-agent received the brief packet, and after `/compact` a resumed session
  still named the file it had read and the path of its saved output (2026-10-24). Security review of the daemon
  (same day) found and listed the gaps recorded under Consequences.

## Context

Brief (help a sub-agent start with what the parent already knows) and carry (keep working state across
`/compact`) both put text into the model's context. Tool output is untrusted: if a packet or snapshot copied it, any
instruction hidden in a file or web page would be repeated into new prompts.

## Decision

1. Both are built **only from facts the daemon observed**: file paths, line counts, edit flags, saved-output ids and
   paths, redacted commands, and the session's own todo text. Never from `tool_response` text, file contents,
   fold excerpts, user prompts or sub-agent prompts.
2. **Brief** runs on `PreToolUse` for the `Agent` tool and appends a block to `prompt` (every other input key is
   unchanged). Packet at most 2,000 bytes, wrapped in `<context-fabric-brief v="0">` and labelled as reference, not
   instructions. Skipped for prompts under 200 or over 12,000 bytes, resumed agents, prompts already briefed, two
   built-in helper agent types, and sessions with no recorded facts.
3. **Carry** snapshots at `PreCompact` to `<state_dir>/carry/<session>.json` (at most 16 KB) and, on `SessionStart`
   with `source: compact`, returns at most 3,000 bytes as `additionalContext`. No transcript parsing in v0.
4. Facts live in memory (`cfxd/session_state.py`: bounded per session, one lock, no I/O under it). Only the carry
   snapshot is written to disk; a daemon restart loses the rest and brief skips.
5. Every decision writes a content-free receipt (`briefs.jsonl`, `carry.jsonl`: counts, sizes, a hash).
   `<state_dir>/config.json` turns each feature on or off (`cfxd set brief|carry on|off`).

## Consequences

- Brief returns `permissionDecision: allow` so that `updatedInput` is honoured, which also skips an "ask" prompt for
  the Agent tool whenever a brief is attached (deny rules still apply). Revisit if Claude Code accepts
  `updatedInput` without it.
- Value is unproven: the packet can be larger than a short task prompt. The planned on/off comparison on real work
  decides the thresholds.
- Command redaction is pattern-based and incomplete; brief and carry list recent commands. See the hardening work
  tracked from the 2026-10-24 security review.
- Hot-path hooks (`PreToolUse`, `PostToolUse`) are plain `http` hooks to a fixed local port and cannot carry a
  secret, so another user on the same machine could read that traffic or answer in the daemon's place. The launcher
  verifies the daemon's identity at session start and warns, but cannot stop the hot path. **Use the alpha on
  single-user computers.** A real fix needs Claude Code to send a per-install header, or the hot path to move to
  command hooks (about 100 ms per call).
