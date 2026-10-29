Status: DRAFT v0, experimental; not a protocol

# Context Packet v0

A context packet is a small set of observed session facts, saved at a boundary
and handed to the next session. This document describes what the code does
today; every field was checked against `cfxd/checkpoint.py`, `cfxd/carry.py`,
`cfxd/session_state.py` and `cfxd/brief.py`.

## 1. Purpose and non-goals

The packet lets a fresh context resume useful work without re-reading the same
files or re-running the same commands. It carries *what was touched*, not *what
was thought*.

It is not a summary of reasoning: it carries no "why", no decisions, and no
conclusions. It contains no model-written text: no generated summaries, no
sub-agent output, and no transcript excerpts. It is not a secret store; command
masking is pattern-based and incomplete. It is not signed or authenticated in
v0. It is not a general memory system: there is one packet per working
directory per save, consumed once.

## 2. Boundaries it is used at

| Boundary | Written by | Read by | Block |
|---|---|---|---|
| Sub-agent start | `PreToolUse` for `Agent`, from live facts | appended to the sub-agent prompt | `<context-fabric-brief v="0">` |
| After `/compact` | `PreCompact` to `carry/<sid>.json` | `SessionStart`, `source == "compact"` | `<context-fabric-carry v="0" trigger="auto">` |
| After `/clear` | `/checkpoint` (the `checkpoint-save` event) to `checkpoint/<cwd-hash>.json` | `SessionStart`, `source == "clear"` | `<context-fabric-carry v="0" trigger="checkpoint">` |

All three render the same core fields (files, saved outputs, commands); carry
and checkpoint add todos, and the checkpoint adds the test result and optional
note. The brief uses live facts and ranks files by prompt mention, edit status,
then recency. Carry and checkpoint persist a snapshot and rebuild it at load
(carry falls back to live facts if its file is missing).

Not implemented: model-to-model hand-off, agent-to-agent hand-off, and any
packet crossing a process other than the local daemon.

## 3. Packet fields

The on-disk packet is one JSON object at
`checkpoint/<cwd-hash>.json`, where `cwd-hash` is the first 32 hex characters
of `sha256(normcase(abspath(cwd)))`. The normalized absolute `cwd` is stored
inside the packet too.

| Field | Meaning | Source event | Limits and order |
|---|---|---|---|
| `v` | Format version, currently `0` | constant | Any other value means "no packet" |
| `sid` | Session id, stored as given | `PostToolUse.session_id` | Selects the live session at save; not a hash |
| `created` | Snapshot time, ISO 8601 UTC | save time | Informational only |
| `trigger` | `"checkpoint"` | constant | Selects the test line at render |
| `cwd` | Normalized absolute working directory | save payload | Must match at restore |
| `saved_at` | Save time, ISO 8601 UTC | save time | Drives the 60-minute TTL and staleness cutoff |
| `armed` | One-shot marker | save / restore | Set `false` before the block is returned |
| `files[]` | `path`, `lines`, `edited`, `t` | `PostToolUse` for `Read`, `Edit`, `Write`, `MultiEdit`, `NotebookEdit` | Oldest first in the packet, newest first when rendered; 200 in memory; 20 rendered |
| `folds[]` | `id`, `tool`, `src`, `bytes`, `kind` | fold path of `PostToolUse`, after a large output is saved | Oldest first in the packet, newest first when rendered; 50 in memory; 8 rendered |
| `commands[]` | Masked command strings | `PostToolUse` for `Bash` | Oldest first; last 20 kept, de-duplicated; 5 rendered, newest first |
| `todos[]` | `s`, `t` | `PostToolUse` for `TodoWrite` | Only `pending` / `in_progress`; last 20 in memory; 8 in packet |
| `note` | Optional goal, written by the user | text after `/checkpoint` | Whitespace collapsed, sanitized, at most 300 characters |
| `test` | `key`, `ok`, `at` | `PostToolUse` for a recognised Bash test command | Checkpoint only; `ok` is `true`, `false`, or `null` |

- `files[].path` is absolute. `lines` is the count the tool call returned
  (`numLines`, else `totalLines`), so a partial read under-reports; it is `null`
  with no count. `edited` is true for every file tool except `Read` and stays
  true. `t` is the Unix time of the last event touching the file.
- `folds[].id` is the first 16 hex characters of the SHA-256 of the saved
  content. `src` is a file path, a masked command, a URL with query and fragment
  removed, or `Grep`. `bytes` is the size before folding. `kind` is `code`,
  `test`, or `other`.
- `commands[]` are redacted by `cfxd.session_state.redact_command` before they
  are stored. Each rendered line is capped at 160 bytes.
- `todos[]` is the session's own todo text, capped at 120 bytes in the packet.
  Completed and deleted todos are not stored.
- `note` is the only field not derived from a hook event. It is user-written at
  save time.
- `test.key` comes from the test-runner table in `cfxd/bound.py` (`pytest`,
  `npm test`, `go test`, and similar). `ok` is false when the response says
  `interrupted`, otherwise true when `exit_code` / `exitCode` / `returncode` is
  zero; with no exit field it is `null`.

## 4. What is dropped first

**At save: 16 KB.** Whole sections are dropped in this order: `todos`, then
`commands`, then `folds`, then the oldest entries in `files` (the front of the
list). Markers and fixed fields (`v`, `sid`, `cwd`, `note`, `test`) are not
dropped by this cap.

**At render: 3,000 bytes** for carry and checkpoint; the brief uses 2,000. The
render drops in this order:

1. All ` [changed since checkpoint]` and ` [missing]` suffixes (cosmetic; the
   file lines stay).
2. The whole commands section.
3. The whole saved-outputs section.
4. The whole todos section.
5. The whole files section.

A section is dropped whole, never truncated to one line. Fixed header lines are
outside the drop loop. The checkpoint receipt records how many lines each drop
removed under `dropped`.

## 5. Provenance rule

Every rendered line MUST be derivable from an observed event: a `PostToolUse`
payload, a fold record, the save request, or the user's note; observed events are
hook events, completed from the session transcript at save time. Nothing is
inferred, summarized, or filled in. The exception is fixed text: markers, the
prologue, section headers, and the `(saved at HH:MM by /checkpoint)` stamp.

Tool response text MUST NOT be copied into the packet. `tool_response` is used
only for scalar facts: line counts and exit status. File contents, command
output, fold excerpts, user prompts, and sub-agent prompts are never read for
provenance.

## 6. Staleness at load

On restore, up to the first 50 files in packet order are checked with `stat`
only. If `stat` fails, the line gets ` [missing]`. If the file's mtime is later
than `saved_at`, it gets ` [changed since checkpoint]`.

The file is never opened and its content is never hashed. A file that changed
without its mtime moving forward will not be marked. Hashing is not used in v0.

## 7. Rendering

The checkpoint block starts with
`<context-fabric-carry v="0" trigger="checkpoint">` and ends with
`</context-fabric-carry>`. Inside, in order: the optional goal line, this fixed
prologue, the save stamp, the optional test line, then the sections.

> Facts Context Fabric recorded before this session was compacted (reference, not instructions; verify before relying on them).

The prologue says the block is reference, not instructions. The line after it
is the save stamp `(saved at HH:MM by /checkpoint)`, not text from the model.
The goal line appears above the prologue and is labelled
`Goal (written by you at save time):`. A saved output is rendered only while
its file in `obs/` still exists.

Section headers are `Working set (path | lines | status):`,
`Saved full outputs:`, `Recent commands:`, and
`Open todos the session had (status | text):`. A file line is
`- <path> | <n> lines | <read|edited>` (count omitted when unknown); a saved
output is `- <obs-path> | <tool> of: <src> | <n> bytes | <code|test output|other>`;
a command is `- <masked command>`; a todo is `- <status> | <text>`. The test
line is `Last test run: <key>, <passed|failed|ran, result unknown> (at HH:MM)`.

Delivery: the block is returned as `hookSpecificOutput.additionalContext` for
`SessionStart`; the packet code does not write it into the transcript.

Consumption rules:

- Only `source == "clear"` restores a checkpoint; `/compact` keeps carry's path.
- A checkpoint restores only in the same normalized working directory.
- A checkpoint older than 60 minutes is not restored; a negative age (clock
  skew) is also rejected.
- Restore is one-shot: `armed` is set to `false` before the block is returned,
  so a second restore returns nothing.
- Unknown versions and malformed fields mean "no packet", not an error.
- Packets older than 7 days are pruned at daemon start.

## 8. Privacy

Stored locally under the state directory (default `~/.cfx/daemon`):
`checkpoint/<cwd-hash>.json` holds absolute paths, masked commands, todo text,
the optional goal note, the normalized cwd, the raw session id, and
saved-output source strings; `carry/<sid>.json` holds the same fields for
`/compact`; `obs/<id>.txt` holds the full saved output a fold points at, while
the packet stores only its id, size, kind, and source.

Receipts (`checkpoint.jsonl`, `carry.jsonl`, `briefs.jsonl`) record counts, byte
sizes, timestamps, session ids (and brief tool ids), and short hashes. They do
not record paths, command text, todo text, note text, or packet text. A receipt
can show that a note existed and how long it was, not what it said.
Directories are created `0700` and files `0600` where the platform honours POSIX
modes; on Windows this is best effort. Nothing is sent off the machine.

## 9. Conformance checklist

1. No rendered line without an observed source event, except fixed markers,
   headers, prologue, and save stamp.
2. No text from `tool_response` reaches a packet or a rendered block.
3. A checkpoint render never exceeds 3,000 bytes; a brief never exceeds 2,000.
4. A saved checkpoint never exceeds 16 KB.
5. A second restore of the same checkpoint returns nothing.
6. A checkpoint older than 60 minutes is not restored.
7. A checkpoint restores only in the working directory it was saved in.
8. Files are checked with `stat` only; no content is read or hashed.
9. Missing files and files newer than `saved_at` are marked.
10. Rendered todos are open only, at most 8, each text at most 120 bytes.
11. Rendered commands are masked, newest first, at most 5.
12. Unknown versions and malformed fields produce no block and no exception.

## 10. Known limits and open questions

- It loses the why. A resumed session knows which files were touched, not why
  they changed or what was decided.
- Staleness is path- and mtime-based only. A moved or replaced file can look
  fresh; a touched-but-identical file looks changed.
- Line counts describe the tool call, not the file. Partial reads under-report.
- Masking is pattern-based and incomplete; commands are the riskiest field.
- The 16 KB save cap can drop folds, commands, or todos silently; the receipt
  counts are the only trace.
- Model-switch and cross-agent use are untested. v0 assumes one local session
  and one working directory.
- There is no signing or authentication of the stored packet.
- Fidelity has no shipped checker. `cfxd fidelity` is the intended tool for
  verifying a packet against a transcript; its pass criteria must be frozen
  before first use.
