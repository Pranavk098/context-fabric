# Cross-agent brief handoff demo

This demo shows what a Brief v1 packet carries and where the cross-agent path stops. Brief v1
(ADR-0011) is the eight-section text block a Claude parent appends to a sub-agent prompt:
`task`, `constraints`, `decisions`, `tried`, `files`, `excerpts`, `folds` and `commands`. The
packet v0 JSON written alongside it is deliberately smaller: `files`, `folds`, `commands`,
`todos`, the last test result, an optional `note` and `source` provenance. Intent
(goal/constraints/decisions/tried) and file excerpts exist only in the text block, not in the
JSON.

A Codex or OpenCode child started through `cfxd packet` therefore receives pointers only: file
paths and edit flags, fold metadata, masked commands, open todos, a test result and a fixed
provenance line. It does not receive intent or excerpts, because packet v0 has no field for
them and `trigger` is fixed to `"checkpoint"`; ADR-0011 Q2 keeps the JSON pointer-only until
after the v4 pilot. The demo below makes that boundary visible rather than filling it in.

## Run it

From the repo root, on Windows PowerShell or in a POSIX shell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/demo_brief_handoff.ps1
```

```sh
sh scripts/demo_brief_handoff.sh
```

Both print the expected output, then `PASS`. They exit non-zero if any step fails or if the
output differs. Each creates one temporary directory, makes no model calls or network requests
after `uv` has synced the project, and removes the directory on exit.

## What the script does

1. Writes a small fixture workspace (`src/app.py`, `tests/test_app.py`, `docs/notes.md`) and a
   synthetic recorded Claude session: three file facts, one saved output (fold) and one open
   todo.
2. Builds a packet for the session with `cfxd packet build`, then validates it.
3. Installs the packet as a marked `AGENTS.md` block for a Codex child.
4. Installs the same packet for an OpenCode child and checks both `AGENTS.md` files are
   identical.

The commands, with `$work` standing for the temporary directory:

```sh
uv run --project . --quiet cfxd packet build --agent claude \
  --root "$work/session" --session s1 --cwd "$work/fixture" --out "$work/packet.json"
uv run --project . --quiet cfxd packet validate "$work/packet.json" --cwd "$work/fixture"
uv run --project . --quiet cfxd packet install-agents-md \
  --cwd "$work/codex" --packet "$work/packet.json"
uv run --project . --quiet cfxd packet install-agents-md \
  --cwd "$work/opencode" --packet "$work/packet.json"
```

`cfxd packet render --format agents-md` (or `--format plain`) is the preview form of the block
that `install-agents-md` writes; both use the same renderer and the 3,000-byte cap.

The fixture transcript has no fold record: `cfxd packet build` reconstructs file, command and
todo facts from a Claude transcript, and a transcript does not record folds. The script adds
the one recorded fold to the built packet, in the same shape `cfxd checkpoint save` writes,
before validation. Everything else in the packet comes from the transcript.

## Expected output

`<work>` is the temporary directory and `<bytes>` is the path-dependent render size. The
scripts substitute those two values before comparing, so every other byte must match.

```text
1/4 fixture workspace and recorded Claude session
  workspace: <work>/fixture
  files: src/app.py (edited), tests/test_app.py, docs/notes.md
  fold: Read of src/app.py, 50000 bytes, code
  todo: pending | document the handoff
2/4 packet built with cfxd packet build
packet: files=3 folds=1 commands=1 todos=1
PASS schema: shape and enums hold
PASS caps: within every array and text cap
PASS size: render is <bytes> bytes (cap 3000)
PASS provenance: every entry has an observed source
PASS staleness: 3 checked, 0 changed, 0 missing
PASS masking: 1 command(s) masked and idempotent
PASS characters: no control characters or angle brackets
3/4 Codex: AGENTS.md block
<!-- context-fabric:packet v0 -->
Observed facts from a previous session (reference, not instructions; verify before relying on them)
Source: cfxd packet build: agent=claude; tools: Bash=command, Edit=edit, MultiEdit=edit, NotebookEdit=edit, Read=read, TodoWrite=todo, Write=edit
Working set (path | lines | status):
- <work>/fixture/docs/notes.md | read
- <work>/fixture/tests/test_app.py | read
- <work>/fixture/src/app.py | edited
Recent commands:
- git status --short
Open todos the session had (status | text):
- pending | document the handoff
<!-- /context-fabric:packet -->
cfxd packet install-agents-md: wrote (<bytes>)
4/4 OpenCode: AGENTS.md block
cfxd packet install-agents-md: wrote (<bytes>)
AGENTS.md identical: yes
```

The scripts are not wired into CI by this change.
