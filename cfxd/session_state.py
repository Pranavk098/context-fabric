"""In-memory session facts for brief/carry: bounded, locked, and content-free.

Records paths, line counts, edit flags, fold ids/paths, redacted commands and
todo text from ``PostToolUse`` events. Nothing survives a daemon restart — the
only thing ever persisted is the carry snapshot (a later task), so a restarted
daemon simply has no facts and brief skips with a ``no-state`` receipt.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any

MAX_SESSIONS = 64
MAX_FILES = 200
MAX_FOLDS = 50
MAX_COMMANDS = 20
MAX_TODOS = 20
IDLE_TTL_S = 24 * 3600

_FILE_TOOLS = ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit")

_LOCK = threading.Lock()


@dataclass(frozen=True)
class FileFact:
    path: str
    lines: int | None
    edited: bool
    last_seen: float


@dataclass(frozen=True)
class FoldFact:
    obs_id: str
    obs_path: str
    tool: str
    source: str
    bytes: int
    kind: str


@dataclass(frozen=True)
class SessionFacts:
    session_id: str
    files: tuple[FileFact, ...]
    folds: tuple[FoldFact, ...]
    commands: tuple[str, ...]
    todos: tuple[tuple[str, str], ...]


class _Session:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.files: OrderedDict[str, FileFact] = OrderedDict()
        self.folds: OrderedDict[str, FoldFact] = OrderedDict()
        self.commands: deque[str] = deque()
        self.todos: list[tuple[str, str]] = []
        self.last_seen = time.time()


_SESSIONS: OrderedDict[str, _Session] = OrderedDict()


def observe_post_tool_use(payload: dict[str, Any]) -> None:
    """Record facts from a ``PostToolUse`` event. Never raises."""
    try:
        _observe(payload)
    except Exception:
        pass


def _observe(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        return
    session_id = payload.get("session_id")
    tool_name = payload.get("tool_name")
    if not isinstance(session_id, str) or not isinstance(tool_name, str):
        return
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    now = time.time()

    # Extract the fact outside the lock; only dict mutation happens under it.
    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            return
        redacted = redact_command(command)
        with _LOCK:
            sess = _session(session_id)
            sess.last_seen = now
            _record_command(sess, redacted)
        return
    if tool_name == "TodoWrite":
        todos = tool_input.get("todos")
        if not isinstance(todos, list):
            return
        parsed = _parse_todos(todos)
        with _LOCK:
            sess = _session(session_id)
            sess.last_seen = now
            sess.todos = parsed
        return
    if tool_name in _FILE_TOOLS:
        path, lines = _file_fact(tool_name, tool_input, payload.get("tool_response"))
        if path is None:
            return
        with _LOCK:
            sess = _session(session_id)
            sess.last_seen = now
            _record_file(sess, path, lines, edited=tool_name != "Read", now=now)


def record_fold(
    session_id: str | None,
    tool: str,
    source: str,
    obs_id: str,
    obs_path: str,
    original_bytes: int,
    kind: str,
) -> None:
    """Record a large saved output (called from ``fold._fold``). Never raises."""
    try:
        if not isinstance(session_id, str):
            return
        with _LOCK:
            sess = _session(session_id)
            sess.last_seen = time.time()
            sess.folds[obs_id] = FoldFact(
                obs_id=obs_id,
                obs_path=obs_path,
                tool=tool,
                source=source,
                bytes=original_bytes,
                kind=kind,
            )
            sess.folds.move_to_end(obs_id)
            while len(sess.folds) > MAX_FOLDS:
                sess.folds.popitem(last=False)
    except Exception:
        pass


def _session(session_id: str) -> _Session:
    """Get or create a session, evicting idle sessions and enforcing the LRU cap."""
    _evict_idle()
    sess = _SESSIONS.get(session_id)
    if sess is None:
        sess = _Session(session_id)
        _SESSIONS[session_id] = sess
    _SESSIONS.move_to_end(session_id)
    while len(_SESSIONS) > MAX_SESSIONS:
        _SESSIONS.popitem(last=False)
    return sess


def _evict_idle() -> None:
    cutoff = time.time() - IDLE_TTL_S
    for sid in [sid for sid, s in _SESSIONS.items() if s.last_seen < cutoff]:
        del _SESSIONS[sid]


def _record_command(sess: _Session, command: str) -> None:
    try:
        sess.commands.remove(command)
    except ValueError:
        pass
    sess.commands.append(command)
    while len(sess.commands) > MAX_COMMANDS:
        sess.commands.popleft()


def _parse_todos(todos: list[Any]) -> list[tuple[str, str]]:
    parsed: list[tuple[str, str]] = []
    for item in todos:
        if not isinstance(item, dict):
            continue
        text = item.get("content")
        status = item.get("status")
        if isinstance(text, str) and isinstance(status, str):
            parsed.append((status, text))
    return parsed[:MAX_TODOS]


def _file_fact(
    tool_name: str, tool_input: dict[str, Any], tool_response: Any
) -> tuple[str | None, int | None]:
    key = "notebook_path" if tool_name == "NotebookEdit" else "file_path"
    path = tool_input.get(key)
    if not isinstance(path, str) or not path.strip():
        return None, None
    lines: int | None = None
    if isinstance(tool_response, dict):
        file = tool_response.get("file")
        if isinstance(file, dict):
            for field in ("totalLines", "numLines"):
                value = file.get(field)
                if isinstance(value, int) and not isinstance(value, bool):
                    lines = value
                    break
    return path, lines


def _record_file(sess: _Session, path: str, lines: int | None, edited: bool, now: float) -> None:
    key = os.path.normcase(os.path.abspath(path))
    existing = sess.files.get(key)
    if lines is None and existing is not None:
        lines = existing.lines
    was_edited = existing.edited if existing is not None else False
    sess.files[key] = FileFact(
        path=os.path.abspath(path), lines=lines, edited=was_edited or edited, last_seen=now
    )
    sess.files.move_to_end(key)
    while len(sess.files) > MAX_FILES:
        sess.files.popitem(last=False)


def snapshot(session_id: str) -> SessionFacts | None:
    """Deep copy of a session's facts, taken under the lock."""
    if not isinstance(session_id, str):
        return None
    with _LOCK:
        sess = _SESSIONS.get(session_id)
        if sess is None:
            return None
        return SessionFacts(
            session_id=sess.session_id,
            files=tuple(sess.files.values()),
            folds=tuple(sess.folds.values()),
            commands=tuple(sess.commands),
            todos=tuple(sess.todos),
        )


def to_json(facts: SessionFacts) -> dict[str, Any]:
    return {
        "v": 0,
        "session_id": facts.session_id,
        "files": [
            {"path": f.path, "lines": f.lines, "edited": f.edited, "last_seen": f.last_seen}
            for f in facts.files
        ],
        "folds": [
            {
                "obs_id": f.obs_id,
                "obs_path": f.obs_path,
                "tool": f.tool,
                "source": f.source,
                "bytes": f.bytes,
                "kind": f.kind,
            }
            for f in facts.folds
        ],
        "commands": list(facts.commands),
        "todos": [{"status": s, "text": t} for s, t in facts.todos],
    }


def from_json(obj: dict[str, Any]) -> SessionFacts | None:
    """Reject unknown versions and bad types; return ``None`` on any mismatch."""
    if not isinstance(obj, dict) or obj.get("v") != 0:
        return None
    session_id = obj.get("session_id")
    if not isinstance(session_id, str):
        return None
    try:
        files = tuple(
            FileFact(
                path=_require_str(item, "path"),
                lines=_require_int_or_none(item.get("lines")),
                edited=_require_bool(item.get("edited")),
                last_seen=_require_float(item.get("last_seen")),
            )
            for item in _require_list(obj.get("files"))
        )
        folds = tuple(
            FoldFact(
                obs_id=_require_str(item, "obs_id"),
                obs_path=_require_str(item, "obs_path"),
                tool=_require_str(item, "tool"),
                source=_require_str(item, "source"),
                bytes=_require_int(item.get("bytes")),
                kind=_require_str(item, "kind"),
            )
            for item in _require_list(obj.get("folds"))
        )
        commands = tuple(_require_str_value(c) for c in _require_list(obj.get("commands")))
        todos = tuple(
            (_require_str(t, "status"), _require_str(t, "text"))
            for t in _require_list(obj.get("todos"))
        )
    except (TypeError, ValueError, KeyError):
        return None
    return SessionFacts(
        session_id=session_id, files=files, folds=folds, commands=commands, todos=todos
    )


def _require_list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError("expected list")
    return value


def _require_str(item: dict[str, Any], key: str) -> str:
    value = item[key]
    if not isinstance(value, str):
        raise TypeError("expected str")
    return value


def _require_str_value(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("expected str")
    return value


def _require_int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("expected int")
    return value


def _require_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    return _require_int(value)


def _require_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise TypeError("expected bool")
    return value


def _require_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("expected float")
    return float(value)


_ASSIGN_SECRET_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:token|secret|key|pass\w*|auth)[A-Za-z0-9_]*)\s*=\s*[^\s'\"]+"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s'\"]+")
_PFLAG_RE = re.compile(r"(?<!\S)-p\S+")
_URL_USERPASS_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://[^/\s@:]+):([^/\s@]+)@")


def redact_command(cmd: str) -> str:
    """Redact secrets from a command: ``NAME=***``, ``Bearer ***``, ``-p***``, ``user:***@``."""
    if not isinstance(cmd, str):
        return ""
    cmd = _ASSIGN_SECRET_RE.sub(r"\1=***", cmd)
    cmd = _BEARER_RE.sub("Bearer ***", cmd)
    cmd = _PFLAG_RE.sub("-p***", cmd)
    cmd = _URL_USERPASS_RE.sub(r"\1:***@", cmd)
    return cmd


def reset() -> None:
    """Clear all sessions (tests only)."""
    with _LOCK:
        _SESSIONS.clear()
