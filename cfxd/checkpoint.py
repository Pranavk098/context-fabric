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
import re
import threading
import time
from collections import deque
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cfxd import carry
from cfxd.bound import test_key_any
from cfxd.brief import _sanitize
from cfxd.fsutil import ensure_dir, open_log
from cfxd.handlers import add_observer, register, register_after
from cfxd.session_state import (
    MAX_COMMANDS,
    MAX_FILES,
    MAX_TODOS,
    FileFact,
    SessionFacts,
    TestFact,
    redact_command,
    snapshot,
)

CHECKPOINT_DIR = "checkpoint"
CHECKPOINT_LOG = "checkpoint.jsonl"
RESTORE_TTL_SECONDS = 60 * 60
MAX_FILE_BYTES = carry.MAX_SNAPSHOT_FILE_BYTES
NOTE_MAX_CHARS = 300
MAX_STALE_STATS = 50
TRANSCRIPT_TAIL_BYTES = 4 * 1024 * 1024
TRANSCRIPT_MAX_AGE_S = 6 * 3600
_PRUNE_TTL_SECONDS = 7 * 24 * 3600
_TRIGGER = "checkpoint"

_NO_SESSION = "Checkpoint not saved: no live session with recorded facts for this directory."
_TRANSCRIPT_SUFFIX = " (completed from the transcript; the helper missed some tool calls)"

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


# --- transcript fallback -------------------------------------------------
# PostToolUse hooks can be dropped under load; the transcript records the same
# events, so save-time facts are rebuilt from its tail when the daemon is thin.

_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_EXIT_CODE_RE = re.compile(r"^\s*Exit code\s+(-?\d+)", re.IGNORECASE)

_FILE_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"})


def _transcript_root() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        return Path(configured) / "projects"
    return Path.home() / ".claude" / "projects"


def _project_dir_name(cwd: str) -> str:
    """Claude Code's project directory: the cwd with non-alphanumerics as ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _transcript_path(cwd: str, session_id: str | None) -> Path | None:
    """The session transcript, else the newest one for this project within 6 hours."""
    try:
        project = _transcript_root() / _project_dir_name(cwd)
        if not project.is_dir():
            return None
        if session_id is not None and _SESSION_ID_RE.fullmatch(session_id) is not None:
            candidate = project / f"{session_id}.jsonl"
            return candidate if candidate.is_file() else None
        cutoff = time.time() - TRANSCRIPT_MAX_AGE_S
        newest: Path | None = None
        newest_mtime = 0.0
        for path in project.iterdir():
            try:
                if not path.is_file() or path.suffix != ".jsonl":
                    continue
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff and mtime > newest_mtime:
                newest, newest_mtime = path, mtime
        return newest
    except OSError:
        return None


def _iter_tail(path: Path) -> Iterator[dict[str, Any]]:
    """Read at most the last 4 MB of a transcript, skipping a partial first line."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            start = max(0, size - TRANSCRIPT_TAIL_BYTES)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return
    if start:
        _, _, data = data.partition(b"\n")
    for raw in data.splitlines():
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def _object_ts(obj: dict[str, Any], msg: Any, fallback: float) -> float:
    for value in (obj.get("timestamp"), msg.get("timestamp") if isinstance(msg, dict) else None):
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    return fallback


def _result_texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
    return []


def _result_ok(block: dict[str, Any]) -> bool:
    """A matching ``tool_result`` is a pass unless it is flagged as a failure."""
    if block.get("is_error") is True:
        return False
    for text in _result_texts(block.get("content")):
        match = _EXIT_CODE_RE.match(text)
        if match is not None:
            return int(match.group(1)) == 0
    return True


def _parse_todos(raw: list[Any]) -> tuple[tuple[str, str], ...]:
    parsed: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = item.get("content")
        status = item.get("status")
        if isinstance(text, str) and isinstance(status, str):
            parsed.append((status, text))
    return tuple(parsed[:MAX_TODOS])


def facts_from_transcript(path: Path) -> SessionFacts | None:
    """Rebuild the daemon's fact shapes from a transcript tail; ``None`` if unreadable."""
    try:
        fallback = path.stat().st_mtime
    except OSError:
        return None
    files: dict[str, FileFact] = {}
    commands: deque[str] = deque()
    todos: tuple[tuple[str, str], ...] = ()
    test: TestFact | None = None
    test_id: str | None = None
    seq = 0

    for obj in _iter_tail(path):
        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        ts = _object_ts(obj, msg, fallback + seq * 1e-6)
        seq += 1

        for block in blocks:
            kind = block.get("type")
            if kind == "tool_use":
                name = block.get("name")
                tool_input = block.get("input")
                if not isinstance(name, str) or not isinstance(tool_input, dict):
                    continue
                bid = block.get("id")
                if name in _FILE_TOOLS:
                    key = "notebook_path" if name == "NotebookEdit" else "file_path"
                    file_path = tool_input.get(key)
                    if not isinstance(file_path, str) or not file_path.strip():
                        continue
                    norm = os.path.normcase(os.path.abspath(file_path))
                    existing = files.pop(norm, None)
                    files[norm] = FileFact(
                        path=os.path.abspath(file_path),
                        lines=existing.lines if existing is not None else None,
                        edited=(existing.edited if existing is not None else False)
                        or name != "Read",
                        last_seen=ts,
                    )
                    while len(files) > MAX_FILES:
                        files.pop(next(iter(files)))
                elif name == "Bash":
                    command = tool_input.get("command")
                    if not isinstance(command, str) or not command.strip():
                        continue
                    redacted = redact_command(command)
                    try:
                        commands.remove(redacted)
                    except ValueError:
                        pass
                    commands.append(redacted)
                    while len(commands) > MAX_COMMANDS:
                        commands.popleft()
                    test_key = test_key_any(command)
                    if test_key is not None:
                        test = TestFact(key=test_key, ok=None, at=ts)
                        test_id = bid if isinstance(bid, str) else None
                elif name == "TodoWrite":
                    raw = tool_input.get("todos")
                    if isinstance(raw, list):
                        todos = _parse_todos(raw)
            elif kind == "tool_result":
                tid = block.get("tool_use_id")
                if tid is not None and tid == test_id and test is not None:
                    test = TestFact(key=test.key, ok=_result_ok(block), at=test.at)

    return SessionFacts(
        session_id=path.stem,
        files=tuple(files.values()),
        folds=(),
        commands=tuple(commands),
        todos=todos,
        test=test,
    )


def _merge_facts(
    daemon: SessionFacts | None, transcript: SessionFacts | None
) -> tuple[SessionFacts | None, str]:
    """Union the two fact sets (daemon wins) and report which source supplied them."""
    daemon_empty = daemon is None or _empty(daemon)
    transcript_empty = transcript is None or _empty(transcript)
    if daemon_empty:
        return (None if transcript_empty else transcript), "transcript"
    assert daemon is not None
    if transcript_empty:
        return daemon, "daemon"
    assert transcript is not None

    files: dict[str, FileFact] = {}
    for fact in daemon.files:
        files[os.path.normcase(os.path.abspath(fact.path))] = fact
    added_file = False
    for fact in transcript.files:
        key = os.path.normcase(os.path.abspath(fact.path))
        if key not in files:
            files[key] = fact
            added_file = True
    merged_files = tuple(sorted(files.values(), key=lambda f: f.last_seen)[-MAX_FILES:])

    commands = deque(daemon.commands)
    added_command = False
    for command in transcript.commands:
        if command not in commands:
            commands.append(command)
            added_command = True
    while len(commands) > MAX_COMMANDS:
        commands.popleft()

    added_todos = not daemon.todos and bool(transcript.todos)
    added_test = daemon.test is None and transcript.test is not None
    merged = SessionFacts(
        session_id=daemon.session_id,
        files=merged_files,
        folds=daemon.folds,
        commands=tuple(commands),
        todos=daemon.todos or transcript.todos,
        test=daemon.test or transcript.test,
    )
    added = added_file or added_command or added_todos or added_test
    return merged, "merged" if added else "daemon"


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
    daemon = snapshot(session_id) if session_id is not None else None
    transcript = None
    try:
        transcript_path = _transcript_path(cwd, session_id)
        if transcript_path is not None:
            transcript = facts_from_transcript(transcript_path)
    except Exception:
        transcript = None
    facts, source = _merge_facts(daemon, transcript)
    if facts is None:
        _receipt(state_dir, session_id, "checkpoint_save", "missing", {}, 0, 0)
        return {"summary": _NO_SESSION}
    if session_id is None:
        session_id = facts.session_id
    obj = carry.snapshot_object(facts, _TRIGGER)
    obj["cwd"] = _norm(cwd)
    obj["saved_at"] = datetime.now(UTC).isoformat()
    obj["armed"] = True
    note = _clean_note(payload.get("note"))
    if note is not None:
        obj["note"] = note
    data = json.dumps(obj).encode("utf-8")
    carry.write_state_file(state_dir, CHECKPOINT_DIR, checkpoint_path(state_dir, cwd).name, data)
    counts = _counts(obj)
    _receipt(
        state_dir,
        session_id,
        "checkpoint_save",
        "saved",
        counts,
        len(data),
        0,
        note_chars=len(note) if note is not None else 0,
        source_info={
            "daemon_files": len(daemon.files) if daemon is not None else 0,
            "transcript_files": len(transcript.files) if transcript is not None else 0,
            "merged_files": len(facts.files),
            "source": source,
        },
    )
    summary = _summary(counts)
    daemon_files = len(daemon.files) if daemon is not None else 0
    if daemon_files * 2 < len(facts.files):
        summary += _TRANSCRIPT_SUFFIX
    return {"summary": summary}


def _clean_note(value: Any) -> str | None:
    """User-written goal: collapse whitespace, sanitise, cap at 300 characters."""
    if not isinstance(value, str):
        return None
    text = " ".join(_sanitize(" ".join(value.split())).split())[:NOTE_MAX_CHARS]
    return text or None


def _stale_marks(obj: dict[str, Any], saved: datetime | None) -> dict[str, str]:
    """Stat (never read) up to 50 listed files; mark missing or changed since save."""
    files = obj.get("files")
    if saved is None or not isinstance(files, list):
        return {}
    cutoff = saved.timestamp()
    marks: dict[str, str] = {}
    for item in files[:MAX_STALE_STATS]:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str):
            continue
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            marks[path] = " [missing]"
            continue
        if mtime > cutoff:
            marks[path] = " [changed since checkpoint]"
    return marks


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
    goal = _clean_note(obj.get("note"))
    text, dropped = carry.render_report(
        facts,
        state_dir,
        _TRIGGER,
        note=_note(saved),
        goal=goal,
        stale=_stale_marks(obj, saved),
    )
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
        note_chars=len(goal) if isinstance(goal, str) else 0,
        dropped=dropped,
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
    saved = _saved_at(obj.get("saved_at"))
    return carry.render_facts(
        facts,
        state_dir,
        _TRIGGER,
        note=_note(saved),
        goal=_clean_note(obj.get("note")),
        stale=_stale_marks(obj, saved),
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
    note_chars: int = 0,
    dropped: dict[str, int] | None = None,
    source_info: dict[str, Any] | None = None,
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
            "note_chars": note_chars,
            "dropped": dropped or {},
        }
        if source_info is not None:
            entry.update(source_info)
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
