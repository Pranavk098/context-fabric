# Implementing Context Packet v0

Status: **DRAFT v0, experimental**. It is a *packet format*, not a protocol.
The normative prose is [`context-packet-v0.md`](context-packet-v0.md); the
machine-checkable form is [`context-packet-v0.schema.json`](context-packet-v0.schema.json);
example files are in [`vectors/`](vectors/). Check a file with
`cfxd packet validate FILE [--cwd DIR] [--json]`.

## What to implement

1. **Writer.** At `/checkpoint`, take the session's live facts and emit one JSON
   object per the schema: `v`, `sid`, `created`, `trigger: "checkpoint"`, `cwd`,
   `saved_at`, `armed: true`, then bounded `files`, `folds`, `commands`, `todos`,
   an optional `note` and `test`. Arrays are oldest first. Apply the caps from
   the writer constants (200 files, 50 folds, 20 commands, 8 todos, 120 UTF-8
   bytes per todo, 300 characters of note) and drop whole sections if the
   snapshot would exceed 16 KB. Mask every command with the project's
   `redact_command` rules before storing it.
2. **Renderer.** On restore, rebuild the `<context-fabric-carry v="0"
   trigger="checkpoint">` block in the documented order, stat (never read) up to
   50 listed files to add ` [missing]` / ` [changed since checkpoint]`, drop
   sections in the documented order until the block is at most 3,000 bytes, and
   set `armed: false` before returning it once.
3. **Reader.** Reject unknown `v`, wrong `cwd`, an unarmed packet, a negative
   age, and an age over 60 minutes by returning nothing — no exception. Prune
   packet files older than 7 days.

Do not infer anything: every line comes from a hook event, a fold record, the
save request, or the user's note. Never copy `tool_response` text into a packet.

## Handoff between agents (experimental)

A packet may be built from another agent's session log instead of a live Claude
Code session: `cfxd packet build --agent claude|codex|opencode` reads only
observed tool calls (file paths, masked commands, todos where that agent records
them, and the last recognised test run) through one per-agent tool-name table;
unknown tool names are ignored, never guessed into another class. The optional
`source` field records which agent and which tool mapping produced the packet.
`cfxd checkpoint import FILE` runs the same checks as `cfxd packet validate`,
rewrites `cwd`, re-arms the packet and writes it as a checkpoint, so the next
`/clear` uses the shipped renderer and caps. `cfxd packet render` and
`cfxd packet install-agents-md` produce a ≤3,000-byte handoff block, or write
only the marked block in `AGENTS.md` (read by Codex and OpenCode; no other agent
is claimed). This path is experimental and has been tested on hand-built logs
only, not end to end with live Codex or OpenCode.

## What NOT to claim

- Not a protocol, not a standard, not interoperable until a second
  implementation exists. Call it a packet format.
- No signing, authentication or tamper evidence in v0.
- No model-written text: no summaries, decisions, transcripts or agent output.
- Not a secret store. Masking is pattern-based and incomplete; commands are the
  riskiest field. The packet is not encrypted and lives under the user's state
  directory.
- No cross-machine or model-switch guarantee inside a live session. The
  cross-agent `packet build` / `checkpoint import` path is experimental, tested
  on hand-built logs only, and is not a protocol.

## Conformance checklist → validator check

| Spec rule | `cfxd packet validate` |
| :--- | :--- |
| Required fields, types, enums, closed objects, `v == 0` | `schema` |
| Array caps, todo/note caps, 16 KB snapshot cap | `caps` |
| Render never exceeds 3,000 bytes (and no data is dropped to fit) | `size` |
| Every file/command/todo/fold/test entry has an observed source | `provenance` |
| Missing or changed listed files are reported (`--cwd`) | `staleness` |
| Commands survive `redact_command` unchanged and idempotently | `masking` |
| No control characters or angle brackets in any string | `characters` |
| One-shot restore, 60-minute TTL, cwd match, 7-day prune | runtime only: not checkable from a stored file |

## How fidelity is measured

`cfxd fidelity` compares Claude Code's compaction summaries with the session
before the boundary: recall of held files, precision of named paths (unseen
mentions are a proxy for invention), and the checkpoint's 20-file recall bound.
The metrics are frozen in [`../FIDELITY-METRICS.md`](../FIDELITY-METRICS.md)
(frozen metrics v1, `M1`–`M3`). They measure summaries, not this packet's
usefulness; no paired saving test has run yet.

## Known gaps

- The packet stores no counts and no provenance hashes. Save/restore counts,
  byte sizes and drop counts live only in `checkpoint.jsonl` receipts, so a
  stored packet cannot prove how it was produced.
- `test.key`, `files[].path`, commands and fold `src` are stored as recorded;
  sanitising (dropping `<`, `>`, control characters) happens at render. A
  hostile file name can therefore be present in the JSON but still render
  safely. The validator rejects such packets because every implementation
  should sanitise before storing.
- JSON Schema cannot express byte caps. Todo text is 120 UTF-8 *bytes* in the
  code but `maxLength: 120` *characters* in the schema; the checker uses bytes.
- The 16 KB cap applies to the snapshot before `cwd`, `saved_at`, `armed` and
  `note` are added, so the final file can be slightly larger. The `caps` check
  mirrors that split.
- `lines` is the line count the tool call returned, not the file's length; a
  partial read under-reports.
- Staleness is path- and mtime-based. A moved or replaced file can look fresh; a
  touched-but-identical file looks changed. No hashing in v0.
- `folds` are schema-validated but the rendered size check only counts a fold
  when a matching `obs/<id>.txt` exists, as at restore.
- Carry packets (`/compact`) share most fields but use trigger `auto`/`manual`
  and have no `cwd`, `saved_at` or `armed`; this schema is checkpoint-only.

Feedback: open an issue at <https://github.com/Pranavk098/context-fabric/issues>
with the packet version, the validator output and, if possible, a vector file.
Tell us if you build a second writer or reader — that is when "packet format"
can become "protocol".
