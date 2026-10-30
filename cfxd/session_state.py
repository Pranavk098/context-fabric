"""In-memory session facts for brief/carry: bounded, locked, and content-free.

Records paths, line counts, edit flags, fold ids/paths, redacted commands and
todo text from ``PostToolUse`` events. Nothing survives a daemon restart — the
only thing ever persisted is the carry snapshot (a later task), so a restarted
daemon simply has no facts and brief skips with a ``no-state`` receipt.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any

from cfxd.bound import test_key_any

MAX_SESSIONS = 64
MAX_FILES = 200
MAX_FOLDS = 50
MAX_COMMANDS = 20
MAX_TODOS = 20
IDLE_TTL_S = 24 * 3600

_FILE_TOOLS = ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit")

# Line ranges for Brief v1 excerpts (ADR-0011 decision 3). Off until the daemon turns it on.
RANGES_PER_FILE = 8
RANGE_MAX_FILE_BYTES = 1_000_000
_RANGES_ENABLED = False

_LOCK = threading.Lock()


@dataclass(frozen=True)
class FileFact:
    path: str
    lines: int | None
    edited: bool
    last_seen: float
    ranges: tuple[tuple[int, int, str, str], ...] = ()


@dataclass(frozen=True)
class FoldFact:
    obs_id: str
    obs_path: str
    tool: str
    source: str
    bytes: int
    kind: str


@dataclass(frozen=True)
class TestFact:
    key: str
    ok: bool | None
    at: float


@dataclass(frozen=True)
class SessionFacts:
    session_id: str
    files: tuple[FileFact, ...]
    folds: tuple[FoldFact, ...]
    commands: tuple[str, ...]
    todos: tuple[tuple[str, str], ...]
    test: TestFact | None = None


class _Session:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.files: OrderedDict[str, FileFact] = OrderedDict()
        self.folds: OrderedDict[str, FoldFact] = OrderedDict()
        self.commands: deque[str] = deque()
        self.todos: list[tuple[str, str]] = []
        self.test: TestFact | None = None
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
        test = _test_fact(command, payload.get("tool_response"), now)
        with _LOCK:
            sess = _session(session_id)
            sess.last_seen = now
            _record_command(sess, redacted)
            if test is not None:
                sess.test = test
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
        ranges: list[tuple[int, int, str, str]] = []
        if _RANGES_ENABLED and _is_parent_event(payload):
            ranges = _file_ranges(tool_name, tool_input, payload.get("tool_response"), path)
        with _LOCK:
            sess = _session(session_id)
            sess.last_seen = now
            _record_file(sess, path, lines, edited=tool_name != "Read", now=now, ranges=ranges)


def _test_fact(command: str, response: Any, now: float) -> TestFact | None:
    key = test_key_any(command)
    if key is None:
        return None
    return TestFact(key=key, ok=_exit_ok(response), at=now)


def _exit_ok(response: Any) -> bool | None:
    """Exit status from the tool response, or ``None`` when the payload has none."""
    if not isinstance(response, dict):
        return None
    if response.get("interrupted") is True:
        return False
    for field in ("exit_code", "exitCode", "returncode"):
        value = response.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            return value == 0
    return None


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
            parsed.append((status, redact_command(text)))
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
            # numLines is what this call returned (a partial read is shorter than the file).
            for field in ("numLines", "totalLines"):
                value = file.get(field)
                if isinstance(value, int) and not isinstance(value, bool):
                    lines = value
                    break
    return path, lines


def set_range_recording(enabled: bool) -> None:
    """Turn line-range recording on or off (the daemon ties it to ``brief_v1``)."""
    global _RANGES_ENABLED
    _RANGES_ENABLED = bool(enabled)


def _is_parent_event(payload: dict[str, Any]) -> bool:
    """False when the payload says a sub-agent made the call (best effort, unverified)."""
    if payload.get("agent_id"):
        return False
    return not (payload.get("isSidechain") is True or payload.get("is_sidechain") is True)


def _disk_lines(path: str) -> tuple[str, list[str]] | None:
    """``(sha16, lines)`` of the file as it is on disk now, or ``None``."""
    try:
        if os.stat(path).st_size > RANGE_MAX_FILE_BYTES:
            return None
        with open(path, "rb") as fh:
            data = fh.read(RANGE_MAX_FILE_BYTES + 1)
    except OSError:
        return None
    if len(data) > RANGE_MAX_FILE_BYTES:
        return None
    text = data.decode("utf-8", "replace").replace("\r\n", "\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return hashlib.sha256(data).hexdigest()[:16], lines


def _file_ranges(
    tool_name: str, tool_input: dict[str, Any], tool_response: Any, path: str
) -> list[tuple[int, int, str, str]]:
    """The ranges one file event touched, hashed from disk (never ``tool_response`` text)."""
    try:
        if tool_name == "NotebookEdit":
            return []
        disk = _disk_lines(os.path.abspath(path))
        if disk is None:
            return []
        sha, lines = disk
        total = len(lines)
        if total == 0:
            return []
        if tool_name == "Read":
            offset = tool_input.get("offset")
            start = offset if isinstance(offset, int) and not isinstance(offset, bool) else 1
            start = max(start, 1)
            count = None
            if isinstance(tool_response, dict) and isinstance(tool_response.get("file"), dict):
                count = tool_response["file"].get("numLines")
            if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                return []
            end = min(start + count - 1, total)
            return [(start, end, sha, "read")] if start <= end else []
        if tool_name == "Write":
            return [(1, total, sha, "edit")]
        if tool_name == "Edit":
            news = [tool_input.get("new_string")]
        else:  # MultiEdit
            edits = tool_input.get("edits")
            news = (
                [e.get("new_string") for e in edits if isinstance(e, dict)]
                if isinstance(edits, list)
                else []
            )
        text = "\n".join(lines)
        out: list[tuple[int, int, str, str]] = []
        for new in news:
            if not isinstance(new, str) or not new.strip("\r\n"):
                continue
            needle = new.replace("\r\n", "\n").rstrip("\n")
            idx = text.find(needle)
            if idx < 0:
                continue
            start = text.count("\n", 0, idx) + 1
            end = start + needle.count("\n")
            out.append((start, min(end, total), sha, "edit"))
        return out
    except Exception:
        return []


def _merge_ranges(
    existing: tuple[tuple[int, int, str, str], ...], new: list[tuple[int, int, str, str]]
) -> tuple[tuple[int, int, str, str], ...]:
    """Newest last; ranges hashed against an older file version are dropped; at most 8."""
    if not new:
        return existing
    sha = new[-1][2]
    kept = [r for r in existing if r[2] == sha]
    for item in new:
        if item in kept:
            kept.remove(item)
        kept.append(item)
    return tuple(kept[-RANGES_PER_FILE:])


def _record_file(
    sess: _Session,
    path: str,
    lines: int | None,
    edited: bool,
    now: float,
    ranges: list[tuple[int, int, str, str]] | None = None,
) -> None:
    key = os.path.normcase(os.path.abspath(path))
    existing = sess.files.get(key)
    if lines is None and existing is not None:
        lines = existing.lines
    was_edited = existing.edited if existing is not None else False
    merged = _merge_ranges(existing.ranges if existing is not None else (), ranges or [])
    sess.files[key] = FileFact(
        path=os.path.abspath(path),
        lines=lines,
        edited=was_edited or edited,
        last_seen=now,
        ranges=merged,
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
            test=sess.test,
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
        "test": (
            None
            if facts.test is None
            else {"key": facts.test.key, "ok": facts.test.ok, "at": facts.test.at}
        ),
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
        test = _parse_test(obj.get("test"))
    except (TypeError, ValueError, KeyError):
        return None
    return SessionFacts(
        session_id=session_id, files=files, folds=folds, commands=commands, todos=todos, test=test
    )


def _parse_test(value: Any) -> TestFact | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("expected test object")
    key = _require_str(value, "key")
    ok = value.get("ok")
    if ok is not None and not isinstance(ok, bool):
        raise TypeError("expected bool or None")
    return TestFact(key=key, ok=ok, at=_require_float(value.get("at")))


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
    r"(?i)\b([A-Za-z0-9_]*(?:token|secret|key|pass\w*|auth)[A-Za-z0-9_]*)"
    r"\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s'\"]+)"
)
_FLAG_SECRET_RE = re.compile(
    r"(?i)(--(?:password|passwd|pass|token|secret|api-key))(\s*=\s*|\s+)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s'\"]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s'\"]+")
_HEADER_SECRET_RE = re.compile(
    r"(?i)\b([A-Za-z0-9-]*(?:token|auth|key|secret)[A-Za-z0-9-]*|cookie|set-cookie)"
    r"\s*:\s*(?!\s)(?!Bearer\b)[^\"'\r\n]+"
)
_AUTH_BEARER_RE = re.compile(r"(?i)((?:proxy-)?authorization\s*:\s*bearer\s*\\?\s*)([^\s'\"]+)")
_JSON_SECRET_RE = re.compile(
    r"(?i)(\"(?:password|passwd|secret|token|api[-_]?key|authorization)\"\s*:\s*\")([^\"]*)(\")"
)
_SET_SECRET_RE = re.compile(
    r"(?i)\b((?:aws\s+configure\s+set|(?:npm|yarn|pnpm)\s+config\s+set)\s+"
    r"\S*(?:token|secret|key|password|auth)\S*\s+)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s'\"]+)"
)
_DOCKER_LOGIN_P_RE = re.compile(r"(?i)(\bdocker\s+login\b[^\n]*?\s-p\s+)(\S+)")
_PSQL_W_RE = re.compile(r"(?i)(\bpsql\b[^\n]*?\s-W\s+)(\S+)")
_OPENSSL_K_RE = re.compile(r"(?i)(\bopenssl\b[^\n]*?\s-k\s+)(\S+)")
_PASS_ARG_RE = re.compile(r"(?i)(\s-pass(?:in)?\s+pass:)(\S+)")
_USER_FLAG_RE = re.compile(r"(?i)(-u|--user)(\s*=\s*|\s+)([^\s'\"]+):([^\s'\"]+)")
# Greedy to the last "@" before the path so multi-@ passwords leave no tail.
_URL_USERPASS_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://[^/\s@:]+:)([^/\s]*@)")
_URL_USER_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s@:]+@")
_BARE_TOKEN_RE = re.compile(
    r"(glpat-[\w-]{20,}|hf_[A-Za-z0-9]{20,}|npm_[A-Za-z0-9]{20,}|"
    r"sk_(?:live|test)_[A-Za-z0-9]{10,}|pypi-[A-Za-z0-9_-]{20,}|"
    r"ghp_\w+|gho_\w+|github_pat_\w+|sk-[\w-]{16,}|xox[abprs]-[\w-]+|"
    r"AKIA[0-9A-Z]{16}|eyJ[\w-]+\.[\w-]+\.[\w-]+)"
)
_P_COMMANDS = frozenset({"mysql", "mysqldump", "sshpass"})
_P_ATTACHED_RE = re.compile(r"(?<!\S)-p[^\s-][^\s]*")
_P_SPACED_RE = re.compile(r"(?<!\S)-p\s+([^-\s][^\s]*)")


def _command_name(cmd: str) -> str:
    for token in cmd.split():
        if "=" in token:
            continue
        return token.replace("\\", "/").rsplit("/", 1)[-1]
    return ""


def _redact_p(cmd: str) -> str:
    if _command_name(cmd) not in _P_COMMANDS:
        return cmd
    cmd = _P_ATTACHED_RE.sub("-p***", cmd)
    return _P_SPACED_RE.sub("-p ***", cmd)


def redact_command(cmd: str) -> str:
    """Redact secrets from a command line; see ``test_cfxd_security`` for the matrix."""
    if not isinstance(cmd, str):
        return ""
    cmd = _ASSIGN_SECRET_RE.sub(r"\1=***", cmd)
    cmd = _FLAG_SECRET_RE.sub(r"\1\2***", cmd)
    cmd = _JSON_SECRET_RE.sub(r"\1***\3", cmd)
    cmd = _SET_SECRET_RE.sub(r"\1***", cmd)
    cmd = _DOCKER_LOGIN_P_RE.sub(r"\1***", cmd)
    cmd = _PSQL_W_RE.sub(r"\1***", cmd)
    cmd = _OPENSSL_K_RE.sub(r"\1***", cmd)
    cmd = _PASS_ARG_RE.sub(r"\1***", cmd)
    cmd = _BEARER_RE.sub("Bearer ***", cmd)
    cmd = _AUTH_BEARER_RE.sub(r"\1***", cmd)
    cmd = _HEADER_SECRET_RE.sub(r"\1: ***", cmd)
    cmd = _USER_FLAG_RE.sub(r"\1\2\3:***", cmd)
    cmd = _URL_USERPASS_RE.sub(r"\1***@", cmd)
    cmd = _URL_USER_RE.sub(r"\1***@", cmd)
    cmd = _BARE_TOKEN_RE.sub("***", cmd)
    return _redact_p(cmd)


def reset() -> None:
    """Clear all sessions (tests only)."""
    with _LOCK:
        _SESSIONS.clear()
