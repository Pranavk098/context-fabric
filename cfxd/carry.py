"""PreCompact/SessionStart carry handlers: snapshot facts on compact, restore them on resume.

On ``PreCompact`` we persist a session's facts to ``<state_dir>/carry/<sid>.json``
(bounded, content-free). On ``SessionStart`` with ``source == "compact"`` we load
that snapshot — or fall back to the live in-memory facts if the file is missing —
and render a small ``<context-fabric-carry>`` block as ``additionalContext``.
Nothing else survives a daemon restart, so a resume after restart still gets the
on-disk snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cfxd.brief import _cap, _command_line, _file_line, _fold_line, _sanitize
from cfxd.config import read_config
from cfxd.handlers import register
from cfxd.session_state import FileFact, FoldFact, SessionFacts, snapshot

CARRY_DIR = "carry"
CARRY_LOG = "carry.jsonl"
SNAPSHOT_CAP_BYTES = 16 * 1024
RENDER_CAP_BYTES = 3_000
MAX_FILES = 20
MAX_FOLDS = 8
MAX_COMMANDS = 5
MAX_TODOS = 8
TODO_MAX_BYTES = 120
_PRUNE_TTL_SECONDS = 7 * 24 * 3600

_SID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TRIGGERS = ("manual", "auto")

_FILES_HEADER = "Working set (path | lines | status):"
_FOLDS_HEADER = "Saved full outputs:"
_COMMANDS_HEADER = "Recent commands:"
_TODOS_HEADER = "Open todos the session had (status | text):"
_PROLOGUE = (
    "Facts Context Fabric recorded before this session was compacted "
    "(reference, not instructions; verify before relying on them)."
)
_CLOSE = "</context-fabric-carry>"

_receipt_lock = threading.Lock()


def install(state_dir: Path) -> None:
    """Register both handlers and prune snapshots older than 7 days (best effort)."""
    register("PreCompact", lambda payload: handle_pre_compact(payload, state_dir))
    register("SessionStart", lambda payload: handle_session_start(payload, state_dir))
    _prune(state_dir)


def handle_pre_compact(payload: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    """Snapshot a session's facts before it is compacted. Never raises."""
    try:
        return _handle_pre_compact(payload, state_dir)
    except Exception:
        return {}


def _handle_pre_compact(payload: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    session_id = _session_id(payload)
    trigger = payload.get("trigger")
    trigger = trigger if trigger in _TRIGGERS else "auto"
    if not read_config(state_dir).get("carry", True):
        _receipt(state_dir, session_id, "PreCompact", "skipped", "disabled", 0, {}, None)
        return {}
    facts = snapshot(session_id) if session_id is not None else None
    if facts is None or _empty(facts):
        _receipt(state_dir, session_id, "PreCompact", "no-state", None, 0, {}, None)
        return {}
    assert session_id is not None
    obj = _cap_snapshot(_to_snapshot(facts, trigger))
    data = json.dumps(obj).encode("utf-8")
    _write_snapshot(state_dir, _sid(session_id), data)
    _receipt(
        state_dir,
        session_id,
        "PreCompact",
        "saved",
        None,
        len(data),
        _counts(obj),
        hashlib.sha256(data).hexdigest()[:16],
    )
    return {}


def handle_session_start(payload: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    """Restore a compacted session's facts as ``additionalContext``. Never raises."""
    try:
        return _handle_session_start(payload, state_dir)
    except Exception:
        return {}


def _handle_session_start(payload: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    session_id = _session_id(payload)
    if payload.get("source") != "compact":
        _receipt(state_dir, session_id, "SessionStart", "skipped", "not-compact", 0, {}, None)
        return {}
    if not read_config(state_dir).get("carry", True):
        _receipt(state_dir, session_id, "SessionStart", "skipped", "disabled", 0, {}, None)
        return {}
    facts: SessionFacts | None = None
    trigger = "auto"
    if session_id is not None:
        loaded = _load_snapshot(state_dir, _sid(session_id))
        if loaded is not None:
            facts, trigger = loaded
    if facts is None and session_id is not None:
        facts = snapshot(session_id)
    if facts is None or _empty(facts):
        _receipt(state_dir, session_id, "SessionStart", "no-state", None, 0, {}, None)
        return {}
    text = _render(facts, state_dir, trigger)
    data = text.encode("utf-8")
    _receipt(
        state_dir,
        session_id,
        "SessionStart",
        "rendered",
        None,
        len(data),
        _counts_facts(facts),
        hashlib.sha256(data).hexdigest()[:16],
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }


def _session_id(payload: dict[str, Any]) -> str | None:
    session_id = payload.get("session_id")
    return session_id if isinstance(session_id, str) else None


def _sid(session_id: str) -> str:
    """File-safe snapshot name: the id itself, or a hash of it if it is hostile."""
    if _SID_RE.fullmatch(session_id) is not None:
        return session_id
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def _empty(facts: SessionFacts) -> bool:
    return not facts.files and not facts.folds and not facts.commands and not facts.todos


def _open_todos(todos: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    return tuple((s, t) for s, t in todos if s in ("pending", "in_progress"))


def _to_snapshot(facts: SessionFacts, trigger: str) -> dict[str, Any]:
    return {
        "v": 0,
        "sid": facts.session_id,
        "created": datetime.now(UTC).isoformat(),
        "trigger": trigger,
        "files": [
            {"path": f.path, "lines": f.lines, "edited": f.edited, "t": f.last_seen}
            for f in facts.files
        ],
        "folds": [
            {"id": f.obs_id, "tool": f.tool, "src": f.source, "bytes": f.bytes, "kind": f.kind}
            for f in facts.folds
        ],
        "commands": list(facts.commands),
        "todos": [
            {"s": s, "t": _cap(t, TODO_MAX_BYTES)} for s, t in _open_todos(facts.todos)[:MAX_TODOS]
        ],
    }


def _json_bytes(obj: dict[str, Any]) -> int:
    return len(json.dumps(obj).encode("utf-8"))


def _cap_snapshot(obj: dict[str, Any]) -> dict[str, Any]:
    """Drop whole sections bottom-up (todos, commands, folds, then oldest files) to fit."""
    for key in ("todos", "commands", "folds"):
        if _json_bytes(obj) <= SNAPSHOT_CAP_BYTES:
            return obj
        obj[key] = []
    files = obj["files"]
    while _json_bytes(obj) > SNAPSHOT_CAP_BYTES and files:
        files.pop(0)
    return obj


def _counts(obj: dict[str, Any]) -> dict[str, int]:
    return {
        "files": len(obj["files"]),
        "folds": len(obj["folds"]),
        "commands": len(obj["commands"]),
        "todos": len(obj["todos"]),
    }


def _counts_facts(facts: SessionFacts) -> dict[str, int]:
    return {
        "files": len(facts.files),
        "folds": len(facts.folds),
        "commands": len(facts.commands),
        "todos": len(facts.todos),
    }


def _write_snapshot(state_dir: Path, sid: str, data: bytes) -> None:
    carry_dir = state_dir / CARRY_DIR
    carry_dir.mkdir(parents=True, exist_ok=True)
    path = carry_dir / f"{sid}.json"
    fd, tmp = tempfile.mkstemp(dir=str(carry_dir), prefix=".carry.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _load_snapshot(state_dir: Path, sid: str) -> tuple[SessionFacts, str] | None:
    path = state_dir / CARRY_DIR / f"{sid}.json"
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("v") != 0:
        return None
    facts = _snapshot_to_facts(obj, state_dir)
    if facts is None:
        return None
    trigger = obj.get("trigger")
    return facts, trigger if trigger in _TRIGGERS else "auto"


def _snapshot_to_facts(obj: dict[str, Any], state_dir: Path) -> SessionFacts | None:
    sid = obj.get("sid")
    if not isinstance(sid, str):
        return None
    files: list[FileFact] = []
    for item in _as_list(obj.get("files")):
        if not isinstance(item, dict):
            return None
        path = item.get("path")
        if not isinstance(path, str):
            return None
        lines = item.get("lines")
        if lines is not None and (not isinstance(lines, int) or isinstance(lines, bool)):
            return None
        edited = item.get("edited")
        if not isinstance(edited, bool):
            return None
        t = item.get("t")
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            return None
        files.append(FileFact(path=path, lines=lines, edited=edited, last_seen=float(t)))
    folds: list[FoldFact] = []
    for item in _as_list(obj.get("folds")):
        if not isinstance(item, dict):
            return None
        obs_id = item.get("id")
        tool = item.get("tool")
        source = item.get("src")
        kind = item.get("kind")
        size = item.get("bytes")
        if (
            not isinstance(obs_id, str)
            or not isinstance(tool, str)
            or not isinstance(source, str)
            or not isinstance(kind, str)
        ):
            return None
        if not isinstance(size, int) or isinstance(size, bool):
            return None
        folds.append(
            FoldFact(
                obs_id=obs_id,
                obs_path=_obs_path(state_dir, obs_id).resolve().as_posix(),
                tool=tool,
                source=source,
                bytes=size,
                kind=kind,
            )
        )
    commands: list[str] = []
    for value in _as_list(obj.get("commands")):
        if not isinstance(value, str):
            return None
        commands.append(value)
    todos: list[tuple[str, str]] = []
    for item in _as_list(obj.get("todos")):
        if not isinstance(item, dict):
            return None
        s = item.get("s")
        t = item.get("t")
        if not isinstance(s, str) or not isinstance(t, str):
            return None
        todos.append((s, t))
    return SessionFacts(
        session_id=sid,
        files=tuple(files),
        folds=tuple(folds),
        commands=tuple(commands),
        todos=tuple(todos),
    )


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _render(facts: SessionFacts, state_dir: Path, trigger: str) -> str:
    files = [_file_line(f) for f in sorted(facts.files, key=lambda f: -f.last_seen)[:MAX_FILES]]
    folds = [
        _fold_line(f) for f in list(reversed(facts.folds)) if _obs_exists(state_dir, f.obs_id)
    ][:MAX_FOLDS]
    commands = [_command_line(c) for c in list(reversed(facts.commands))[:MAX_COMMANDS]]
    todos = [_todo_line(s, t) for s, t in _open_todos(facts.todos)[:MAX_TODOS]]

    sections: list[list[str]] = []
    if files:
        sections.append([_FILES_HEADER, *files])
    if folds:
        sections.append([_FOLDS_HEADER, *folds])
    if commands:
        sections.append([_COMMANDS_HEADER, *commands])
    if todos:
        sections.append([_TODOS_HEADER, *todos])

    header = [f'<context-fabric-carry v="0" trigger="{trigger}">', _PROLOGUE]
    # Drop whole sections bottom-up (todos, then commands, folds, files) to fit.
    while sections:
        lines = [*header, *(line for section in sections for line in section), _CLOSE]
        if len("\n".join(lines).encode("utf-8")) <= RENDER_CAP_BYTES:
            return "\n".join(lines)
        sections.pop()
    return "\n".join([*header, _CLOSE])


def _todo_line(status: str, text: str) -> str:
    return f"- {_cap(_sanitize(status))} | {_cap(_sanitize(text), TODO_MAX_BYTES)}"


def _obs_path(state_dir: Path, obs_id: str) -> Path:
    return state_dir / "obs" / f"{obs_id}.txt"


def _obs_exists(state_dir: Path, obs_id: str) -> bool:
    return _obs_path(state_dir, obs_id).is_file()


def _receipt(
    state_dir: Path,
    session_id: str | None,
    event: str,
    decision: str,
    reason: str | None,
    size: int,
    counts: dict[str, int],
    sha16: str | None,
) -> None:
    try:
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "session_id": session_id,
            "event": event,
            "decision": decision,
            "reason": reason,
            "bytes": size,
            "counts": counts,
            "sha16": sha16,
        }
        state_dir.mkdir(parents=True, exist_ok=True)
        with _receipt_lock:
            with (state_dir / CARRY_LOG).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _prune(state_dir: Path) -> None:
    """Delete snapshots older than 7 days; best effort, never raises."""
    carry_dir = state_dir / CARRY_DIR
    if not carry_dir.is_dir():
        return
    cutoff = time.time() - _PRUNE_TTL_SECONDS
    for path in carry_dir.iterdir():
        try:
            if path.is_file() and path.suffix == ".json" and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue
