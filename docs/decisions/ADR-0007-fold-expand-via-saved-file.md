# ADR-0007 — Fold on the hook path; expand by reading a saved file

- **Status:** Accepted
- **Date:** 2026-10-22
- **Evidence:** `tests/test_cfxd_fold.py`; live `claude -p` runs with the plugin loaded (2026-10-22, Windows):
  a 51,269-byte `Read` folded to 4,360 bytes, and the model found a value on line 4,439 of the file
  through its own tools and answered correctly.

## Context

Plan item C1 (fold) said large tool output becomes a stub that expands through an MCP tool (`cfx expand`).
The hook spike showed `PostToolUse` can replace a tool's output, and that a bare stub confused the model.
`cfxd` must stay standard library only (ADR-0006), so it can't reuse the kernel store behind the existing MCP
`expand` tool.

## Decision

1. `PostToolUse` folds `Read` (`file.content`), `Bash` (`stdout`, `stderr`), `Grep` (`content`) and `WebFetch`
   (`result`) when a field is at least 20,000 bytes. A `Read` with `offset`/`limit` is never folded.
2. The full output is saved byte-exact to `<state_dir>/obs/<sha256[:16]>.txt` (content-addressed; files unused for
   7 days are pruned at daemon start).
3. The replacement is a header that says what happened, where the full copy is, and to use `Read`/`Grep` on it,
   followed by a deterministic excerpt: failure lines and the tail for test output, changed lines for diffs, an
   outline of definitions with line numbers for code, and head + tail otherwise.
4. **Expansion uses the model's own `Read` and `Grep` on the saved file. No MCP expand tool.** Reads and greps
   inside `obs/` are never folded again.
5. Each fold appends sizes, kind and id (never content) to `<state_dir>/folds.jsonl`.

## Consequences

- No MCP server to install or start; works the same for OpenCode and (once it can replace output) Codex.
- The model can `Grep` a huge log instead of pulling all of it back.
- Saved outputs are copies of tool output on the user's disk under `~/.cfx/daemon/obs/`, like Claude Code's own
  transcripts. Pruning keeps them bounded by time, not size; revisit if testers see large directories.
- Excerpts are capped at 4,000 bytes, so very large outlines are cut short; the header says how to see the rest.
- Token savings are not measured yet: the dogfood on/off comparison decides whether 20,000 bytes is the right
  threshold and whether a fold costs more turns than it saves.
