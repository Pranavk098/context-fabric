# Tester guide: `cfxd fidelity`

## What it does
`cfxd fidelity` scans your local Claude Code transcripts for `/compact` boundaries and compares the file paths named in each compaction summary against the files the session actually read or edited beforehand. It reports counts and rates only—how many touched or edited files were kept or dropped, and how many named paths never appeared earlier in the session.

## Commands to run
```bash
uv run cfxd checkup
uv run cfxd fidelity --since 30d
uv run cfxd fidelity --since all
```
*(Note: `--share` is not available in this build on `cfxd fidelity`; the default output already prints aggregate numbers and counts only, with no paths, names, summary text, or hashes.)*

## What the output means
- **Frozen metrics v1 (M1, M2, M3):**
  - `M1 unsupported path rate`: share of path mentions in compaction summaries that never appeared earlier in tool inputs or results.
  - `M2 dropped edit rate`: share of files edited before compaction that the summary did not name.
  - `M3 unsupported pass claims`: summaries claiming tests passed when the last observed test command before compaction failed or none ran.
- **Recall and precision distributions:** median, p25, p75, and mean across compaction events, plus a per-compaction numeric line (`#1 auto: held X files...`).

## What to check by hand
1. Check whether `Compaction boundaries analysed` is greater than 0 (if 0, run `/compact` in a session or run `uv run cfxd fidelity --since all`).
2. Compare `Recall of held files` with `Recall of the 20 most recent files` and check the `By trigger` (`auto` vs `manual`) lines.
3. After a `/compact` in your own work, check by hand whether an edited file you still needed was dropped or whether an unseen path came from your conversation rather than a tool call.

## What to send back
- Send the numeric output of `uv run cfxd fidelity --since 30d` (and `uv run cfxd week --share` if reporting weekly).
- Numbers only: never paste session prompts, summary text, file paths, or commands.

## Known limits
- **Proxy, not a verdict:** path mentions are a proxy for state retention; a summary can keep useful state without naming a file, or name a valid file mentioned only in conversation.
- **No saving is proven:** `fidelity` is a descriptive measurement tool only, and active features such as Brief are opt-in and experimental with no proven saving (E-1 found re-discovery is a small share of total session cost on the maintainer's machine: "1% to 3%, one machine", so Brief is a test, not a promise).
- **Platform status:** Windows needs Git for Windows for plugin hooks; macOS is not verified by the founder.
- **Flags and retired features:** `cfxd fidelity --share` is not available in this build (use default `uv run cfxd fidelity --since 30d` or `uv run cfxd checkup`); `fold`, `bound`, and `checkpoint` are retired and are not part of testing.
