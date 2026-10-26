"""Experimental checkpoint: save a working-state package on request, restore it after ``/clear``.

Claude Code offers plugins no way to run ``/clear``, so the user runs ``/checkpoint``
and then ``/clear`` themselves. ``/checkpoint`` sends ``checkpoint-save``; the daemon
snapshots the live facts of the session active in that cwd and writes them with the
same bounded, content-free writer carry uses to ``<state_dir>/checkpoint/<cwd-hash>.json``.
On the next ``SessionStart`` with ``source == "clear"`` the package is rendered with
carry's renderer once (``armed`` is cleared atomically), within 60 minutes of saving.
Nothing here runs for ``compact``: that keeps carry's own path.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cfxd import carry
from cfxd.fsutil import ensure_dir, open_log
from cfxd.handlers import add_observer, register, register_after
from cfxd.session_state import SessionFacts, snapshot

CHECKPOINT_DIR = "checkpoint"
CHECKPOINT_LOG = "checkpoint.jsonl"
RESTORE_TTL_SECONDS = 60 * 60
MAX_FILE_BYTES = carry.MAX_SNAPSHOT_FILE_BYTES
_PRUNE_TTL_SECONDS = 7 * 24 * 3600
_TRIGGER = "checkpoint"

_NO_SESSION = "Checkpoint not saved: no live session with recorded facts for this directory."

# session_id of the most recent tool call seen in each cwd (in memory only).
_last_active: dict[str, tuple[int, str]] = {}
_seq = 0

_lock = threading.Lock()
_receipt_lock = threading.Lock()


def install(state_dir: Path) -> None:
    """Register the save event and the ``clear`` restore, and prune old packages."""
    register("checkpoint-save", lambda payload: handle_checkpoint_save(payload, state_dir))
    register_after("SessionStart", lambda payload: handle_session_start(payload, state_dir))
    add_observer("PostToolUse", observe_cwd)
    _prune(state_dir)


def reset() -> None:
    """Clear the in-memory cwd map (tests only)."""
    global _seq
    with _lock:
        _last_active.clear()
        _seq = 0


def observe_cwd(payload: dict[str, Any]) -> None:
    """Remember which session last acted in a cwd. Never raises."""
    try:
        if not isinstance(payload, dict):
            return
        cwd = payload.get("cwd")
        session_id = payload.get("session_id")
        if isinstance(cwd, str) and cwd.strip() and isinstance(session_id, str) and session_id:
            _remember(cwd, session_id)
    except Exception:
        pass


def _remember(cwd: str, session_id: str) -> None:
    global _seq
    with _lock:
        _seq += 1
        _last_active[_cwd_key(cwd)] = (_seq, session_id)


def _cwd_key(cwd: str) -> str:
    return hashlib.sha256(_norm(cwd).encode("utf-8")).hexdigest()[:32]


def _norm(cwd: str) -> str:
    # Same lexical form on every platform; Windows also folds case.
    return os.path.normcase(os.path.abspath(cwd))


def checkpoint_path(state_dir: Path, cwd: str) -> Path:
    return state_dir / CHECKPOINT_DIR / f"{_cwd_key(cwd)}.json"


def _session_for_cwd(cwd: str) -> str | None:
    with _lock:
        entry = _last_active.get(_cwd_key(cwd))
    return entry[1] if entry is not None else None


def handle_checkpoint_save(payload: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    """Save the session's facts as a checkpoint. Never raises."""
    try:
        return _save(payload, state_dir)
    except Exception:
        return {"summary": _NO_SESSION}


def _save(payload: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    if not isinstance(cwd, str) or not cwd.strip():
        _receipt(state_dir, None, "checkpoint_save", "missing", {}, 0, 0)
        return {"summary": _NO_SESSION}
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = _session_for_cwd(cwd)
    else:
        _remember(cwd, session_id)
    facts = snapshot(session_id) if session_id is not None else None
    if facts is None or _empty(facts):
        _receipt(state_dir, session_id, "checkpoint_save", "missing", {}, 0, 0)
        return {"summary": _NO_SESSION}
    obj = carry.snapshot_object(facts, _TRIGGER)
    obj["cwd"] = _norm(cwd)
    obj["saved_at"] = datetime.now(UTC).isoformat()
    obj["armed"] = True
    data = json.dumps(obj).encode("utf-8")
    carry.write_state_file(state_dir, CHECKPOINT_DIR, checkpoint_path(state_dir, cwd).name, data)
    counts = _counts(obj)
    _receipt(state_dir, session_id, "checkpoint_save", "saved", counts, len(data), 0)
    return {"summary": _summary(counts)}


def _summary(counts: dict[str, int]) -> str:
    return (
        f"Checkpoint saved: {counts['files']} files, {counts['commands']} commands, "
        f"{counts['todos']} todos"
    )


def _empty(facts: SessionFacts) -> bool:
    return not facts.files and not facts.folds and not facts.commands and not facts.todos


def _counts(obj: dict[str, Any]) -> dict[str, int]:
    return {
        "files": len(obj.get("files", ())),
        "folds": len(obj.get("folds", ())),
        "commands": len(obj.get("commands", ())),
        "todos": len(obj.get("todos", ())),
    }


def handle_session_start(payload: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    """Restore a checkpoint on ``source == "clear"`` only. Never raises."""
    try:
        return _restore(payload, state_dir)
    except Exception:
        if isinstance(payload, dict):
            _receipt(state_dir, _sid(payload), "checkpoint_restore", "missing", {}, 0, 0)
        return {}


def _restore(payload: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("source") != "clear":
        return {}
    session_id = _sid(payload)
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        _receipt(state_dir, session_id, "checkpoint_restore", "missing", {}, 0, 0)
        return {}
    with _lock:
        return _restore_locked(state_dir, session_id, cwd)


def _restore_locked(state_dir: Path, session_id: str | None, cwd: str) -> dict[str, Any]:
    path = checkpoint_path(state_dir, cwd)
    obj = _read(path)
    if obj is None:
        _receipt(state_dir, session_id, "checkpoint_restore", "missing", {}, 0, 0)
        return {}
    if obj.get("cwd") != _norm(cwd):
        _receipt(state_dir, session_id, "checkpoint_restore", "other_cwd", {}, 0, 0)
        return {}
    saved = _saved_at(obj.get("saved_at"))
    age = time.time() - saved.timestamp() if saved is not None else None
    if age is None or age < 0 or age > RESTORE_TTL_SECONDS:
        _receipt(
            state_dir,
            session_id,
            "checkpoint_restore",
            "expired",
            {},
            0,
            max(0, int(age or 0)),
        )
        return {}
    if obj.get("armed") is not True:
        _receipt(state_dir, session_id, "checkpoint_restore", "disarmed", {}, 0, 0)
        return {}
    facts = carry.facts_from_snapshot(obj, state_dir)
    if facts is None:
        _receipt(state_dir, session_id, "checkpoint_restore", "missing", {}, 0, 0)
        return {}
    text = carry.render_facts(facts, state_dir, _TRIGGER, note=_note(saved))
    # Consume once: disarm before handing the block back, atomically.
    obj["armed"] = False
    carry.write_state_file(state_dir, CHECKPOINT_DIR, path.name, json.dumps(obj).encode("utf-8"))
    data = text.encode("utf-8")
    _receipt(
        state_dir,
        session_id,
        "checkpoint_restore",
        "restored",
        _counts(obj),
        len(data),
        int(age),
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }


def show_text(state_dir: Path, cwd: str) -> str | None:
    """The block Claude would receive, or ``None`` if no valid checkpoint exists."""
    obj = _read(checkpoint_path(state_dir, cwd))
    if obj is None or obj.get("cwd") != _norm(cwd):
        return None
    facts = carry.facts_from_snapshot(obj, state_dir)
    if facts is None:
        return None
    return carry.render_facts(
        facts, state_dir, _TRIGGER, note=_note(_saved_at(obj.get("saved_at")))
    )


def clear(state_dir: Path, cwd: str) -> bool:
    """Delete this cwd's checkpoint; ``True`` if one existed."""
    try:
        checkpoint_path(state_dir, cwd).unlink()
        return True
    except OSError:
        return False


def _sid(payload: dict[str, Any]) -> str | None:
    value = payload.get("session_id")
    return value if isinstance(value, str) else None


def _read(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) and obj.get("v") == 0 else None


def _saved_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        saved = datetime.fromisoformat(value)
    except ValueError:
        return None
    return saved if saved.tzinfo is not None else saved.replace(tzinfo=UTC)


def _note(saved: datetime | None) -> str:
    stamp = saved.astimezone().strftime("%H:%M") if saved is not None else "an unknown time"
    return f"(saved at {stamp} by /checkpoint)"


def _receipt(
    state_dir: Path,
    session_id: str | None,
    event: str,
    outcome: str,
    counts: dict[str, int],
    size: int,
    age: int,
) -> None:
    """One content-free line: counts, byte sizes and age only — no paths or text."""
    try:
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "session_id": session_id,
            "event": event,
            "outcome": outcome,
            "bytes": size,
            "age_s": age,
            "counts": counts,
        }
        ensure_dir(state_dir)
        with _receipt_lock:
            with open_log(state_dir / CHECKPOINT_LOG) as fh:
                fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _prune(state_dir: Path) -> None:
    """Delete checkpoints older than 7 days; best effort, never raises."""
    directory = state_dir / CHECKPOINT_DIR
    if not directory.is_dir():
        return
    cutoff = time.time() - _PRUNE_TTL_SECONDS
    for path in directory.iterdir():
        try:
            if path.is_file() and path.suffix == ".json" and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue
