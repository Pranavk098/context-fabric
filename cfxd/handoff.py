"""Experimental cross-agent handoff: build, render and import a Context Packet v0.

A packet is built from an *existing* agent session log (Claude Code transcript,
Codex rollout, OpenCode database) using only observed tool calls: file paths,
masked commands, todos where the agent records them, and the last recognised
test run. No message text is read. Tool names differ per agent, so every reader
goes through :data:`TOOL_ROLES`; the mapping used is written into the packet's
optional ``source`` field.

The other direction reuses the native checkpoint: ``import`` validates a packet,
rewrites its ``cwd``, arms it and writes it where the next ``/clear`` reads it,
so the restore path, renderer and caps are the ones ``cfxd.checkpoint`` already
ships. ``install-agents-md`` writes the same facts as a marked block in
``AGENTS.md``, which Codex and OpenCode both read; nothing more is claimed.

Tested on hand-built logs only, never end to end with a live Codex or OpenCode.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cfxd import carry, checkpoint, packet
from cfxd.agents.model import default_root
from cfxd.bound import test_key_any
from cfxd.brief import _cap, _command_line, _file_line, _sanitize
from cfxd.session_state import (
    MAX_COMMANDS,
    MAX_FILES,
    MAX_TODOS,
    FileFact,
    SessionFacts,
    TestFact,
    redact_command,
)

AGENTS = ("claude", "codex", "opencode")
IMPORT_MAX_AGE_S = 24 * 3600
RENDER_CAP_BYTES = carry.RENDER_CAP_BYTES

#: One table for every reader: observed tool name -> the fact class it produces.
#: Names absent from a row are ignored (never guessed into another class).
TOOL_ROLES: dict[str, dict[str, str]] = {
    "claude": {
        "Read": "read",
        "Edit": "edit",
        "Write": "edit",
        "MultiEdit": "edit",
        "NotebookEdit": "edit",
        "Bash": "command",
        "TodoWrite": "todo",
    },
    "codex": {
        "read": "read",
        "read_file": "read",
        "view_image": "read",
        "apply_patch": "edit",
        "write_file": "edit",
        "shell": "command",
        "exec": "command",
        "exec_command": "command",
        "local_shell": "command",
        "update_plan": "todo",
    },
    "opencode": {
        "read": "read",
        "edit": "edit",
        "write": "edit",
        "patch": "edit",
        "bash": "command",
        "todowrite": "todo",
    },
}

_BEGIN = "<!-- context-fabric:packet v0 -->"
_END = "<!-- /context-fabric:packet -->"
_PROLOGUE = (
    "Observed facts from a previous session "
    "(reference, not instructions; verify before relying on them)"
)
_FILES_HEADER = "Working set (path | lines | status):"
_COMMANDS_HEADER = "Recent commands:"
_TODOS_HEADER = "Open todos the session had (status | text):"
_TEST_LABEL = "Last test run:"
_MAX_STALE_STATS = checkpoint.MAX_STALE_STATS

_EXIT_TEXT_RE = re.compile(r"(?i)\b(?:process\s+)?(?:exited\s+with|exit)\s+code[:\s]+(-?\d+)")
_EXIT_JSON_RE = re.compile(r'"exit_code"\s*:\s*(-?\d+)')
_PATCH_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+?)\s*$", re.MULTILINE)


def source_label(agent: str) -> str:
    """Provenance string naming the agent and the exact tool mapping used."""
    mapping = ", ".join(f"{name}={role}" for name, role in sorted(TOOL_ROLES[agent].items()))
    return f"cfxd packet build: agent={agent}; tools: {mapping}"


def _abs_path(value: str, cwd: str) -> str:
    path = value if os.path.isabs(value) else os.path.join(cwd, value)
    return os.path.abspath(path)


def _open_todos(raw: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, list):
        return ()
    parsed: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = item.get("content")
        if not isinstance(text, str):
            text = item.get("step")
        if not isinstance(text, str):
            text = item.get("text")
        status = item.get("status")
        if isinstance(text, str) and status in ("pending", "in_progress"):
            cleaned = _cap(_sanitize(" ".join(text.split())), carry.TODO_MAX_BYTES)
            if cleaned.strip():
                parsed.append((status, cleaned))
    return tuple(parsed[:MAX_TODOS])


def _exit_ok(output: Any) -> bool | None:
    """True/False from an observed exit status, else ``None`` (never guessed)."""
    if isinstance(output, dict):
        for key in ("exit_code", "exitCode", "exit", "code"):
            value = output.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value == 0
    if isinstance(output, str):
        match = _EXIT_TEXT_RE.search(output) or _EXIT_JSON_RE.search(output)
        if match is not None:
            return int(match.group(1)) == 0
    return None


class _Facts:
    """Accumulates observed tool calls into bounded, content-free facts."""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.files: dict[str, FileFact] = {}
        self.commands: deque[str] = deque()
        self.todos: tuple[tuple[str, str], ...] = ()
        self.test: TestFact | None = None
        self.test_call: str | None = None

    def file(self, value: Any, ts: float, edited: bool) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        full = _sanitize(_abs_path(value, self.cwd))
        if not full:
            return
        key = os.path.normcase(full)
        existing = self.files.pop(key, None)
        self.files[key] = FileFact(
            path=full,
            lines=existing.lines if existing is not None else None,
            edited=(existing.edited if existing is not None else False) or edited,
            last_seen=ts,
        )
        while len(self.files) > MAX_FILES:
            self.files.pop(next(iter(self.files)))

    def command(self, value: Any, ts: float, call_id: str | None = None) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        redacted = _sanitize(redact_command(value))
        if not redacted.strip():
            return False
        try:
            self.commands.remove(redacted)
        except ValueError:
            pass
        self.commands.append(redacted)
        while len(self.commands) > MAX_COMMANDS:
            self.commands.popleft()
        key = test_key_any(value)
        if key is not None:
            self.test = TestFact(key=key, ok=None, at=ts)
            self.test_call = call_id
            return True
        return False

    def test_result(self, call_id: str | None, output: Any) -> None:
        if self.test is None or call_id is None or call_id != self.test_call:
            return
        self.test = TestFact(key=self.test.key, ok=_exit_ok(output), at=self.test.at)

    def facts(self, session_id: str) -> SessionFacts:
        return SessionFacts(
            session_id=session_id,
            files=tuple(sorted(self.files.values(), key=lambda fact: fact.last_seen)),
            folds=(),
            commands=tuple(self.commands),
            todos=self.todos,
            test=self.test,
        )


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _newest(paths: list[Path]) -> Path | None:
    best: Path | None = None
    best_mtime = -1.0
    for path in paths:
        stamp = _mtime(path)
        if stamp > best_mtime:
            best, best_mtime = path, stamp
    return best


# --- Claude Code transcript ------------------------------------------------


def _claude_log(root: Path | None, cwd: str, session: str | None) -> Path | None:
    sid = session if session and session != "latest" else None
    if root is None:
        found = checkpoint._transcript_path(cwd, sid)
        if found is not None:
            return found
        base = checkpoint._transcript_root()
    else:
        base = root
    if not base.is_dir():
        return None
    if sid is not None:
        wanted = sid if checkpoint._SESSION_ID_RE.fullmatch(sid) else ""
        for path in base.rglob("*.jsonl"):
            if wanted and path.stem == wanted:
                return path
        return None
    return _newest([path for path in base.rglob("*.jsonl") if path.is_file()])


def _claude_facts(root: Path | None, session: str | None, cwd: str) -> SessionFacts | None:
    path = _claude_log(root, cwd, session)
    if path is None:
        return None
    return checkpoint.facts_from_transcript(path)


# --- Codex rollout ---------------------------------------------------------


def _codex_base(root: Path | None) -> Path:
    return root if root is not None else default_root("codex")


def _codex_files(root: Path | None) -> list[tuple[Path, bool]]:
    base = _codex_base(root)
    found: list[tuple[Path, bool]] = []
    if not base.is_dir():
        return found
    for path in base.rglob("rollout-*"):
        if not path.is_file():
            continue
        if path.name.endswith(".jsonl.zst"):
            found.append((path, True))
        elif path.name.endswith(".jsonl"):
            found.append((path, False))
    found.sort(key=lambda item: _mtime(item[0]), reverse=True)
    return found


def _codex_args(payload: dict[str, Any]) -> Any:
    raw = payload.get("arguments")
    if raw is None:
        raw = payload.get("input")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return raw
        return parsed
    return None


def _command_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        # Codex wraps shell scripts as ["bash", "-lc", "<script>"]; keep the script.
        if len(value) >= 3 and value[1] in ("-lc", "-c"):
            script = value[-1]
            return script if isinstance(script, str) else None
        return " ".join(value)
    return None


def _file_key(args: dict[str, Any]) -> Any:
    for key in ("file_path", "filePath", "path"):
        value = args.get(key)
        if isinstance(value, str):
            return value
    return None


def _patch_paths(args: Any) -> list[str]:
    if not isinstance(args, dict):
        return []
    for key in ("input", "patch", "patchText"):
        value = args.get(key)
        if isinstance(value, str):
            return _PATCH_RE.findall(value)
    return []


def _codex_facts(root: Path | None, session: str | None, cwd: str) -> SessionFacts | None:
    zmod: Any = None
    for path, is_zst in _codex_files(root):
        if is_zst and zmod is None:
            from cfxd.agents import codex

            zmod = codex._zstd_open()
            if zmod is None:
                continue
        facts = _read_codex(path, is_zst, zmod, cwd)
        if session and session != "latest" and facts.session_id != session:
            continue
        return facts
    return None


def _read_codex(path: Path, is_zst: bool, zmod: Any, cwd: str) -> SessionFacts:
    from cfxd.agents import codex

    session_id = path.name
    acc = _Facts(cwd)
    calls: dict[str, tuple[str, str | None]] = {}
    seq = 0
    for obj in codex._iter_lines(path, is_zst, zmod):
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = obj.get("type")
        ts = _iso_ts(obj.get("timestamp")) or _mtime(path) + seq * 1e-6
        seq += 1
        if kind == "session_meta":
            sid = payload.get("session_id")
            if isinstance(sid, str) and sid:
                session_id = sid
            continue
        if kind != "response_item":
            continue
        rtype = payload.get("type")
        if rtype in ("function_call", "custom_tool_call"):
            name = payload.get("name")
            if not isinstance(name, str):
                continue
            role = TOOL_ROLES["codex"].get(name)
            call_id = payload.get("call_id")
            cid = call_id if isinstance(call_id, str) else None
            calls[cid or ""] = (role or "", name)
            if role is None:
                continue
            args = _codex_args(payload)
            if role in ("read", "edit"):
                if isinstance(args, dict):
                    key = _file_key(args)
                    if isinstance(key, str):
                        acc.file(key, ts, role == "edit")
                    else:
                        for patch_path in _patch_paths(args):
                            acc.file(patch_path, ts, role == "edit")
                elif isinstance(args, str):
                    for match in _PATCH_RE.finditer(args):
                        acc.file(match.group(1), ts, role == "edit")
            elif role == "command":
                command = _command_text(args.get("command")) if isinstance(args, dict) else None
                if command is None and isinstance(args, dict):
                    command = _command_text(args.get("cmd"))
                acc.command(command, ts, cid)
            elif role == "todo":
                raw = args.get("plan") if isinstance(args, dict) else None
                if raw is None and isinstance(args, dict):
                    raw = args.get("todos")
                acc.todos = _open_todos(raw)
        elif rtype in ("function_call_output", "custom_tool_call_output"):
            call_id = payload.get("call_id")
            cid = call_id if isinstance(call_id, str) else None
            if cid is not None and calls.get(cid, ("", ""))[0] == "command":
                acc.test_result(cid, payload.get("output"))
    return acc.facts(session_id)


# --- OpenCode database -----------------------------------------------------

_OPENCODE_FILES = ("filePath", "path", "file")


def _opencode_db(root: Path | None) -> Path:
    if root is None:
        return default_root("opencode")
    return root / "opencode.db" if root.is_dir() else root


def _opencode_session(con: sqlite3.Connection, session: str | None) -> str | None:
    order = "time_updated DESC" if "time_updated" in _columns(con, "session") else "id DESC"
    rows = con.execute(f'SELECT id FROM "session" ORDER BY {order}').fetchall()
    ids = [row[0] for row in rows if isinstance(row[0], str)]
    if not ids:
        return None
    if session and session != "latest":
        return session if session in ids else None
    return ids[0]


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.OperationalError:
        return set()


def _opencode_facts(root: Path | None, session: str | None, cwd: str) -> SessionFacts | None:
    db = _opencode_db(root)
    if not db.is_file():
        return None
    from cfxd.agents import opencode

    con = opencode._connect(db)
    try:
        if opencode._detect_family(con) != "v1":
            return None
        sid = _opencode_session(con, session)
        if sid is None:
            return None
        acc = _Facts(cwd)
        ts_col = "time_created" if "time_created" in _columns(con, "part") else "NULL"
        sql = (
            "SELECT data, " + ts_col + ' FROM "part" WHERE session_id = ? '
            "ORDER BY " + ts_col + ", id"
        )
        for data, stamp in con.execute(sql, (sid,)):
            if not isinstance(data, str):
                continue
            try:
                part = json.loads(data)
            except ValueError:
                continue
            if not isinstance(part, dict) or part.get("type") != "tool":
                continue
            name = part.get("tool")
            if not isinstance(name, str):
                continue
            role = TOOL_ROLES["opencode"].get(name)
            if role is None:
                continue
            ts = _ms(stamp) or time.time()
            state = part.get("state")
            state = state if isinstance(state, dict) else {}
            tool_input = state.get("input")
            tool_input = tool_input if isinstance(tool_input, dict) else {}
            if role in ("read", "edit"):
                path = _file_key(tool_input)
                if isinstance(path, str):
                    acc.file(path, ts, role == "edit")
                else:
                    for patch_path in _patch_paths(tool_input):
                        acc.file(patch_path, ts, role == "edit")
            elif role == "command":
                command = _command_text(tool_input.get("command"))
                if acc.command(command, ts):
                    metadata_ok = _exit_ok(state.get("metadata"))
                    ok = metadata_ok if metadata_ok is not None else _exit_ok(state.get("output"))
                    if acc.test is not None:
                        acc.test = TestFact(key=acc.test.key, ok=ok, at=ts)
            elif role == "todo":
                acc.todos = _open_todos(tool_input.get("todos"))
        return acc.facts(sid)
    finally:
        con.close()


def _ms(value: Any) -> float | None:
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return value / 1000.0
        except (OverflowError, OSError):
            return None
    return None


def _iso_ts(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


# --- build -----------------------------------------------------------------


def build_packet(agent: str, root: Path | None, session: str | None, cwd: str) -> dict[str, Any]:
    """Build a v0 packet from one observed session log. Raises ``LookupError``."""
    if agent == "claude":
        facts = _claude_facts(root, session, cwd)
    elif agent == "codex":
        facts = _codex_facts(root, session, cwd)
    elif agent == "opencode":
        facts = _opencode_facts(root, session, cwd)
    else:
        raise ValueError(f"unknown agent: {agent}")
    if facts is None:
        raise LookupError(f"no {agent} session log found")
    obj = carry.snapshot_object(facts, packet.TRIGGER)
    obj["cwd"] = checkpoint._norm(cwd)
    obj["saved_at"] = datetime.now(UTC).isoformat()
    obj["armed"] = True
    obj["source"] = source_label(agent)
    return obj


# --- import ----------------------------------------------------------------


def import_packet(obj: Any, state_dir: Path, cwd: str, *, force: bool = False) -> dict[str, int]:
    """Validate, re-arm and write a packet as this directory's checkpoint."""
    checks = packet.validate(obj)
    if not packet.passed(checks):
        failed = next(check for check in checks if check.status == packet.FAIL)
        raise ValueError(f"packet is not valid: {failed.name}: {failed.reason}")
    saved = checkpoint._saved_at(obj.get("saved_at"))
    if saved is None:
        raise ValueError("packet has no usable saved_at")
    age = time.time() - saved.timestamp()
    if age > IMPORT_MAX_AGE_S and not force:
        raise ValueError("packet is older than 24 hours (use --force to import it)")
    rewritten = dict(obj)
    rewritten["cwd"] = checkpoint._norm(cwd)
    rewritten["saved_at"] = datetime.now(UTC).isoformat()
    rewritten["armed"] = True
    data = json.dumps(rewritten).encode("utf-8")
    target = checkpoint.checkpoint_path(state_dir, cwd)
    carry.write_state_file(state_dir, checkpoint.CHECKPOINT_DIR, target.name, data)
    return checkpoint._counts(rewritten)


# --- render ----------------------------------------------------------------


def _stale_marks(obj: dict[str, Any], cwd: str | None) -> dict[str, str]:
    if cwd is None:
        return {}
    saved = checkpoint._saved_at(obj.get("saved_at"))
    files = obj.get("files")
    if saved is None or not isinstance(files, list):
        return {}
    cutoff = saved.timestamp()
    marks: dict[str, str] = {}
    for item in files[:_MAX_STALE_STATS]:
        if not isinstance(item, dict):
            continue
        raw = item.get("path")
        if not isinstance(raw, str):
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = Path(cwd) / path
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            marks[raw] = " [missing]"
            continue
        if mtime > cutoff:
            marks[raw] = " [changed since checkpoint]"
    return marks


def _todo_line(status: str, text: str) -> str:
    return f"- {_cap(_sanitize(status))} | {_cap(_sanitize(text), carry.TODO_MAX_BYTES)}"


def render_packet(
    obj: Any, fmt: str = "agents-md", cwd: str | None = None
) -> tuple[str, dict[str, int]]:
    """Render a packet as a compact handoff block; raises ``ValueError``."""
    facts = carry.facts_from_snapshot(obj, Path.cwd())
    if facts is None:
        raise ValueError("the renderer cannot read this packet")
    marks = _stale_marks(obj, cwd)
    ranked = sorted(facts.files, key=lambda fact: -fact.last_seen)[: carry.MAX_FILES]
    files = [_file_line(fact, marks.get(fact.path, "")) for fact in ranked]
    commands = [_command_line(c) for c in list(facts.commands)[-carry.MAX_COMMANDS :]][::-1]
    todos = [_todo_line(s, t) for s, t in carry._open_todos(facts.todos)[: carry.MAX_TODOS]]

    sections: list[tuple[str, list[str]]] = []
    if files:
        sections.append(("files", [_FILES_HEADER, *files]))
    if commands:
        sections.append(("commands", [_COMMANDS_HEADER, *commands]))
    if todos:
        sections.append(("todos", [_TODOS_HEADER, *todos]))

    header = [_PROLOGUE]
    source = obj.get("source")
    if isinstance(source, str) and source:
        header.append(f"Source: {_cap(_sanitize(source), 200)}")
    if facts.test is not None:
        header.append(carry._test_line(facts.test))

    dropped = {"stale": 0, "commands": 0, "todos": 0, "files": 0}

    def body() -> list[str]:
        return [*header, *(line for _, lines in sections for line in lines)]

    def assemble() -> str:
        text = "\n".join(body())
        if fmt == "plain":
            return text
        return f"{_BEGIN}\n{text}\n{_END}"

    def over_cap() -> bool:
        return len(assemble().encode("utf-8")) > RENDER_CAP_BYTES

    def drop_section(name: str) -> bool:
        nonlocal sections
        for index, (key, lines) in enumerate(sections):
            if key == name:
                dropped[key] += len(lines) - 1
                sections = [*sections[:index], *sections[index + 1 :]]
                return True
        return False

    while over_cap():
        if any(marks.get(fact.path) for fact in ranked):
            dropped["stale"] += sum(1 for fact in ranked if marks.get(fact.path))
            marks = {}
            files = [_file_line(fact) for fact in ranked]
            sections = [
                ("files", [_FILES_HEADER, *files]) if key == "files" else (key, lines)
                for key, lines in sections
            ]
            continue
        if drop_section("commands") or drop_section("todos") or drop_section("files"):
            continue
        break
    return assemble(), dropped


# --- AGENTS.md -------------------------------------------------------------


def _bounds(text: str) -> tuple[int, int] | None:
    """Marker span, ``None`` when absent; raises when the markers are damaged."""
    starts = text.count(_BEGIN)
    ends = text.count(_END)
    if starts == 0 and ends == 0:
        return None
    if starts != 1 or ends != 1:
        raise ValueError("AGENTS.md has damaged context-fabric markers")
    start = text.index(_BEGIN)
    end = text.index(_END)
    if end < start:
        raise ValueError("AGENTS.md has damaged context-fabric markers")
    return start, end + len(_END)


def install_agents_md(cwd: str, block: str | None) -> tuple[str, int]:
    """Insert/replace (``block``) or remove (``None``) only the marked block."""
    path = Path(cwd) / "AGENTS.md"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    bounds = _bounds(text)
    if block is None:
        if bounds is None:
            return "unchanged", len(text.encode("utf-8"))
        start, end = bounds
        prefix, suffix = text[:start], text[end:]
        if suffix.startswith("\n"):
            suffix = suffix[1:]
        if prefix.endswith("\n"):
            prefix = prefix[:-1]
        new = prefix + suffix
        path.write_text(new, encoding="utf-8")
        return "removed", len(new.encode("utf-8"))
    if bounds is None:
        new = (text + "\n" if text else "") + block + "\n"
    else:
        start, end = bounds
        new = text[:start] + block + text[end:]
    path.write_text(new, encoding="utf-8")
    return "wrote", len(new.encode("utf-8"))
