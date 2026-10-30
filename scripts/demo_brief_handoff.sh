#!/bin/sh
# One-command, offline demo: a recorded Claude session becomes a Context Packet v0,
# then the same packet is installed as AGENTS.md for Codex and OpenCode.
# Usage: sh scripts/demo_brief_handoff.sh   (needs uv; works in Git Bash and on macOS/Linux)
# Prints PASS or FAIL and exits non-zero on any mismatch. Uses only a temporary directory.

set -eu

here=$(cd "$(dirname "$0")/.." && pwd)
if work_root=$(mktemp -d 2>/dev/null); then :; else
  work_root="${TMPDIR:-/tmp}/cfx-brief-handoff-$$"
  mkdir -p "$work_root"
fi
work=$(cd "$work_root" && { pwd -W 2>/dev/null || pwd; })
rm -rf "$work"
fixture=$work/fixture
mkdir -p "$fixture/src" "$fixture/tests" "$fixture/docs" "$work/session" "$work/codex" "$work/opencode"
trap 'rm -rf "$work"' EXIT

fail() { echo "FAIL: $1"; exit 1; }

command -v uv >/dev/null 2>&1 || fail "uv not found (install: https://docs.astral.sh/uv/)"
# Sync once outside the captured output so a fresh checkout prints only demo output.
uv run --project "$here" --quiet python -c "import cfxd" >/dev/null 2>&1 \
  || fail "uv could not sync the project"

cfx() { uv run --project "$here" --quiet cfxd "$@"; }

uv run --project "$here" --quiet python - "$fixture" "$work" <<'PY' || fail "fixture setup failed"
import hashlib
import json
import pathlib
import sys

fixture = pathlib.Path(sys.argv[1])
work = pathlib.Path(sys.argv[2])

(fixture / "src" / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
(fixture / "tests" / "test_app.py").write_text(
    "from app import run\n\n\ndef test_run():\n    assert run() == 1\n", encoding="utf-8"
)
(fixture / "docs" / "notes.md").write_text("# Notes\n\nHandoff demo.\n", encoding="utf-8")

app = (fixture / "src" / "app.py").as_posix()
tests = (fixture / "tests" / "test_app.py").as_posix()
notes = (fixture / "docs" / "notes.md").as_posix()


def record(kind, timestamp, content):
    return {"type": kind, "timestamp": timestamp, "message": {"role": kind, "content": content}}


def tool(call_id, name, tool_input, timestamp):
    return record(
        "assistant",
        timestamp,
        [{"type": "tool_use", "id": call_id, "name": name, "input": tool_input}],
    )


lines = [
    record("user", "2026-01-02T00:00:00Z", [{"type": "text", "text": "Build the handoff demo."}]),
    tool("t1", "Read", {"file_path": app}, "2026-01-02T00:00:01Z"),
    tool("t2", "Edit", {"file_path": app}, "2026-01-02T00:00:02Z"),
    tool("t3", "Read", {"file_path": tests}, "2026-01-02T00:00:03Z"),
    tool("t4", "Read", {"file_path": notes}, "2026-01-02T00:00:04Z"),
    tool("t5", "Bash", {"command": "git status --short"}, "2026-01-02T00:00:05Z"),
    tool(
        "t6",
        "TodoWrite",
        {
            "todos": [
                {"content": "document the handoff", "status": "pending"},
                {"content": "write the demo", "status": "completed"},
            ]
        },
        "2026-01-02T00:00:06Z",
    ),
]
session = work / "session" / "s1.jsonl"
session.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

# One recorded saved output (a fold). The packet build step below re-adds it because
# a Claude transcript has no fold record; its id is the SHA-256 prefix of its bytes.
content = ("x" * 99 + "\n") * 500
fold_id = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
assert fold_id == "7d95df91bef22e0f", fold_id
(work / "obs").mkdir()
(work / "obs" / f"{fold_id}.txt").write_text(content, encoding="utf-8")
PY

status=0
(
  echo "1/4 fixture workspace and recorded Claude session"
  echo "  workspace: $fixture"
  echo "  files: src/app.py (edited), tests/test_app.py, docs/notes.md"
  echo "  fold: Read of src/app.py, 50000 bytes, code"
  echo "  todo: pending | document the handoff"

  echo "2/4 packet built with cfxd packet build"
  cfx packet build --agent claude --root "$work/session" --session s1 \
    --cwd "$fixture" --out "$work/packet.json" 2>"$work/build.err" \
    || { echo "cfxd packet build failed:"; cat "$work/build.err"; exit 1; }
  uv run --project "$here" --quiet python - "$work/packet.json" "$fixture/src/app.py" <<'PY' || exit 1
import json
import sys

path, source = sys.argv[1:3]
with open(path, encoding="utf-8") as fh:
    obj = json.load(fh)
obj["folds"] = [
    {"id": "7d95df91bef22e0f", "tool": "Read", "src": source, "bytes": 50000, "kind": "code"}
]
with open(path, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(obj, fh, indent=2)
    fh.write("\n")
print(
    f"packet: files={len(obj['files'])} folds={len(obj['folds'])} "
    f"commands={len(obj['commands'])} todos={len(obj['todos'])}"
)
PY
  cfx packet validate "$work/packet.json" --cwd "$fixture" || exit 1

  echo "3/4 Codex: AGENTS.md block"
  install_out=$(cfx packet install-agents-md --cwd "$work/codex" --packet "$work/packet.json") \
    || exit 1
  cat "$work/codex/AGENTS.md"
  echo "$install_out"

  echo "4/4 OpenCode: AGENTS.md block"
  install_out=$(cfx packet install-agents-md --cwd "$work/opencode" --packet "$work/packet.json") \
    || exit 1
  echo "$install_out"
  if cmp -s "$work/codex/AGENTS.md" "$work/opencode/AGENTS.md"; then
    echo "AGENTS.md identical: yes"
  else
    echo "AGENTS.md identical: no"
    exit 1
  fi
) >"$work/actual.raw" 2>&1 || status=$?

if [ "$status" -ne 0 ]; then
  echo "FAIL: a demo step exited $status"
  cat "$work/actual.raw"
  exit 1
fi

uv run --project "$here" --quiet python - "$work/actual.raw" "$work/actual.txt" "$work" <<'PY'
import pathlib
import re
import sys

raw, out, work = sys.argv[1:4]
text = pathlib.Path(raw).read_text(encoding="utf-8").replace("\r\n", "\n")
text = text.replace("\\", "/").replace(work.replace("\\", "/"), "<work>")
text = re.sub(r"render is \d+ bytes", "render is <bytes> bytes", text)
text = re.sub(r"wrote \(\d+ bytes\)", "wrote (<bytes>)", text)
pathlib.Path(out).write_text(text, encoding="utf-8", newline="\n")
PY

cat >"$work/expected.txt" <<'EOF'
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
EOF

if ! cmp -s "$work/expected.txt" "$work/actual.txt"; then
  echo "FAIL: output did not match the expected output"
  diff -u "$work/expected.txt" "$work/actual.txt" || true
  exit 1
fi

cat "$work/actual.txt"
echo "PASS: brief handoff demo matched the expected output."
