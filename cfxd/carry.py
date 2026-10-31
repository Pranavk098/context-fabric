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
from cfxd.fsutil import ensure_dir, open_log
from cfxd.handlers import register
from cfxd.session_state import (
    RANGES_PER_FILE,
    FileFact,
    FoldFact,
    SessionFacts,
    TestFact,
    snapshot,
)

CARRY_DIR = "carry"
CARRY_LOG = "carry.jsonl"
SNAPSHOT_CAP_BYTES = 16 * 1024
MAX_SNAPSHOT_FILE_BYTES = 64 * 1024
RENDER_CAP_BYTES = 3_000
CARRY_V2_CAP_BYTES = 4_000
MAX_FILES = 20
MAX_FOLDS = 8
MAX_COMMANDS = 5
MAX_TODOS = 8
TODO_MAX_BYTES = 120
MAX_CONSTRAINTS = 5
CONSTRAINT_MAX_BYTES = 160
MAX_RANGES_PER_FILE = 3
_PRUNE_TTL_SECONDS = 7 * 24 * 3600

_SID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_FOLD_ID_RE = re.compile(r"[0-9a-f]{16}")
_TRIGGERS = ("manual", "auto")

_FILES_HEADER = "Working set (path | lines | status):"
_FOLDS_HEADER = "Saved full outputs:"
_COMMANDS_HEADER = "Recent commands:"
_TODOS_HEADER = "Open todos the session had (status | text):"
_CONSTRAINTS_HEADER = "Constraints from your earlier messages:"
_GOAL_LABEL = "Goal (written by you at save time):"
_TEST_LABEL = "Last test run:"
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
    constraints = _constraints(payload.get("transcript_path"), facts)
    obj = _cap_snapshot(_to_snapshot(facts, trigger, constraints))
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
    constraints: tuple[str, ...] = ()
    if session_id is not None:
        loaded = _load_snapshot(state_dir, _sid(session_id))
        if loaded is not None:
            facts, trigger, constraints = loaded
    if facts is None and session_id is not None:
        facts = snapshot(session_id)
    if facts is None or _empty(facts):
        _receipt(state_dir, session_id, "SessionStart", "no-state", None, 0, {}, None)
        return {}
    text, _dropped = _render(facts, state_dir, trigger, constraints=constraints)
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


def _constraints(transcript_path: Any, facts: SessionFacts) -> tuple[str, ...]:
    """The user's constraint sentences, sanitised and capped, from allowed prose only."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return ()
    try:
        from cfxd.brief_intent import extract_intent

        intent = extract_intent(transcript_path, "", facts)
    except Exception:
        return ()
    out = []
    for item in intent.constraints[:MAX_CONSTRAINTS]:
        text = _cap(_sanitize(item.text), CONSTRAINT_MAX_BYTES).strip()
        if text:
            out.append(text)
    return tuple(out)


def _to_snapshot(
    facts: SessionFacts, trigger: str, constraints: tuple[str, ...] = ()
) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "v": 0,
        "sid": facts.session_id,
        "created": datetime.now(UTC).isoformat(),
        "trigger": trigger,
        "files": [
            {
                "path": f.path,
                "lines": f.lines,
                "edited": f.edited,
                "t": f.last_seen,
                **(
                    {"rng": [[start, end, kind] for start, end, _sha, kind in f.ranges]}
                    if f.ranges
                    else {}
                ),
            }
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
    if facts.test is not None:
        obj["test"] = {"key": facts.test.key, "ok": facts.test.ok, "at": facts.test.at}
    if constraints:
        obj["constraints"] = list(constraints[:MAX_CONSTRAINTS])
    return obj


def _json_bytes(obj: dict[str, Any]) -> int:
    return len(json.dumps(obj).encode("utf-8"))


def _cap_snapshot(obj: dict[str, Any]) -> dict[str, Any]:
    """Drop whole sections bottom-up (todos, commands, folds, then oldest files) to fit."""
    for key in ("todos", "commands", "folds", "constraints"):
        if _json_bytes(obj) <= SNAPSHOT_CAP_BYTES:
            return obj
        if key in obj:
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


def write_state_file(state_dir: Path, subdir: str, name: str, data: bytes) -> None:
    """Atomically write ``<state_dir>/<subdir>/<name>`` as 0600 (best effort)."""
    directory = state_dir / subdir
    ensure_dir(directory)
    path = directory / name
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=f".{subdir}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _write_snapshot(state_dir: Path, sid: str, data: bytes) -> None:
    write_state_file(state_dir, CARRY_DIR, f"{sid}.json", data)


def snapshot_object(
    facts: SessionFacts, trigger: str, constraints: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Bounded, content-free snapshot dict for ``facts`` (the on-disk carry format)."""
    return _cap_snapshot(_to_snapshot(facts, trigger, constraints))


def facts_from_snapshot(obj: dict[str, Any], state_dir: Path) -> SessionFacts | None:
    """Rebuild facts from a snapshot dict; ``None`` if any field is malformed."""
    return _snapshot_to_facts(obj, state_dir)


def render_facts(
    facts: SessionFacts,
    state_dir: Path,
    trigger: str,
    note: str | None = None,
    goal: str | None = None,
    stale: dict[str, str] | None = None,
    constraints: tuple[str, ...] = (),
    version: int | None = None,
) -> str:
    """Render the carry block; ``note`` is one extra line under the prologue."""
    return _render(
        facts,
        state_dir,
        trigger,
        note=note,
        goal=goal,
        stale=stale,
        constraints=constraints,
        version=version,
    )[0]


def render_report(
    facts: SessionFacts,
    state_dir: Path,
    trigger: str,
    note: str | None = None,
    goal: str | None = None,
    stale: dict[str, str] | None = None,
    constraints: tuple[str, ...] = (),
    version: int | None = None,
) -> tuple[str, dict[str, int]]:
    """Render the block plus the counts of lines dropped to fit the cap."""
    return _render(
        facts,
        state_dir,
        trigger,
        note=note,
        goal=goal,
        stale=stale,
        constraints=constraints,
        version=version,
    )


def _load_snapshot(state_dir: Path, sid: str) -> tuple[SessionFacts, str, tuple[str, ...]] | None:
    path = state_dir / CARRY_DIR / f"{sid}.json"
    try:
        if path.stat().st_size > MAX_SNAPSHOT_FILE_BYTES:
            return None
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("v") != 0:
        return None
    facts = _snapshot_to_facts(obj, state_dir)
    if facts is None:
        return None
    trigger = obj.get("trigger")
    return facts, trigger if trigger in _TRIGGERS else "auto", _snapshot_constraints(obj)


def _snapshot_constraints(obj: dict[str, Any]) -> tuple[str, ...]:
    raw = obj.get("constraints")
    if not isinstance(raw, list):
        return ()
    out = [
        _cap(_sanitize(value), CONSTRAINT_MAX_BYTES).strip()
        for value in raw
        if isinstance(value, str)
    ]
    return tuple(text for text in out if text)[:MAX_CONSTRAINTS]


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
        ranges = _snapshot_ranges(item.get("rng"))
        if ranges is None:
            return None
        files.append(
            FileFact(path=path, lines=lines, edited=edited, last_seen=float(t), ranges=ranges)
        )
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
        if _FOLD_ID_RE.fullmatch(obs_id) is None:
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
    test: TestFact | None = None
    raw_test = obj.get("test")
    if raw_test is not None:
        if not isinstance(raw_test, dict):
            return None
        key = raw_test.get("key")
        ok = raw_test.get("ok")
        at = raw_test.get("at")
        if not isinstance(key, str) or (ok is not None and not isinstance(ok, bool)):
            return None
        if isinstance(at, bool) or not isinstance(at, (int, float)):
            return None
        test = TestFact(key=key, ok=ok, at=float(at))
    return SessionFacts(
        session_id=sid,
        files=tuple(files),
        folds=tuple(folds),
        commands=tuple(commands),
        todos=tuple(todos),
        test=test,
    )


def _snapshot_ranges(value: Any) -> tuple[tuple[int, int, str, str], ...] | None:
    """Parse ``[[start, end, kind], ...]``; ``None`` on malformed input, absent means none."""
    if value is None:
        return ()
    if not isinstance(value, list):
        return None
    out: list[tuple[int, int, str, str]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 3:
            return None
        start, end, kind = item
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not isinstance(kind, str)
            or start < 1
            or end < start
        ):
            return None
        out.append((start, end, "", kind))
    return tuple(out[-RANGES_PER_FILE:])


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _render(
    facts: SessionFacts,
    state_dir: Path,
    trigger: str,
    note: str | None = None,
    goal: str | None = None,
    stale: dict[str, str] | None = None,
    constraints: tuple[str, ...] = (),
    version: int | None = None,
) -> tuple[str, dict[str, int]]:
    # Checkpoint keeps the v1 block byte-for-byte; real compactions get carry v2 only
    # when the carry_v2 flag is on.
    if version is None:
        v2 = trigger != "checkpoint" and read_config(state_dir).get("carry_v2", False)
        version = 2 if v2 else 1
    if version == 1:
        return _render_v1(facts, state_dir, trigger, note=note, goal=goal, stale=stale)
    return _render_v2(
        facts,
        state_dir,
        trigger,
        note=note,
        goal=goal,
        stale=stale,
        constraints=constraints,
    )


def _render_v2(
    facts: SessionFacts,
    state_dir: Path,
    trigger: str,
    note: str | None = None,
    goal: str | None = None,
    stale: dict[str, str] | None = None,
    constraints: tuple[str, ...] = (),
) -> tuple[str, dict[str, int]]:
    """Edited files first (with line ranges), then test, todos, constraints, folds, commands."""
    edited = sorted((f for f in facts.files if f.edited), key=lambda f: -f.last_seen)
    read = sorted((f for f in facts.files if not f.edited), key=lambda f: -f.last_seen)
    ranked = [*edited[:MAX_FILES]]
    ranked.extend(read[: max(0, MAX_FILES - len(ranked))])
    marks = dict(stale or {})

    def file_lines() -> list[str]:
        return [
            _file_line(
                f,
                (_range_suffix(f) if f.edited else "") + marks.get(f.path, ""),
            )
            for f in ranked
        ]

    files = file_lines()
    test = _test_line(facts.test) if facts.test is not None else None
    todos = [_todo_line(s, t) for s, t in _open_todos(facts.todos)[:MAX_TODOS]]
    constraint_lines = [
        f"- {_cap(_sanitize(text), CONSTRAINT_MAX_BYTES)}"
        for text in constraints[:MAX_CONSTRAINTS]
        if _cap(_sanitize(text), CONSTRAINT_MAX_BYTES).strip()
    ]
    folds = [
        _fold_line(f) for f in list(reversed(facts.folds)) if _obs_exists(state_dir, f.obs_id)
    ][:MAX_FOLDS]
    commands = [_command_line(c) for c in list(reversed(facts.commands))[:MAX_COMMANDS]]

    sections: list[tuple[str, list[str]]] = []
    if files:
        sections.append(("files", [_FILES_HEADER, *files]))
    if test is not None:
        sections.append(("test", [test]))
    if todos:
        sections.append(("todos", [_TODOS_HEADER, *todos]))
    if constraint_lines:
        sections.append(("constraints", [_CONSTRAINTS_HEADER, *constraint_lines]))
    if folds:
        sections.append(("folds", [_FOLDS_HEADER, *folds]))
    if commands:
        sections.append(("commands", [_COMMANDS_HEADER, *commands]))

    header = [f'<context-fabric-carry v="0" trigger="{trigger}">']
    if goal:
        header.append(f"{_GOAL_LABEL} {goal}")
    header.append(_PROLOGUE)
    if note is not None:
        header.append(note)

    dropped = {"stale": 0, "commands": 0, "folds": 0, "todos": 0, "constraints": 0, "files": 0}

    def assemble() -> list[str]:
        return [*header, *(line for _, lines in sections for line in lines), _CLOSE]

    def over_cap() -> bool:
        return len("\n".join(assemble()).encode("utf-8")) > CARRY_V2_CAP_BYTES

    def drop_section(name: str) -> bool:
        nonlocal sections
        for index, (key, lines) in enumerate(sections):
            if key == name:
                dropped[key] += len(lines) - 1
                sections = [*sections[:index], *sections[index + 1 :]]
                return True
        return False

    def trim_file() -> bool:
        nonlocal sections
        for index, (key, lines) in enumerate(sections):
            if key == "files" and len(lines) > 1:
                dropped["files"] += 1
                sections = [*sections[:index], ("files", lines[:-1]), *sections[index + 1 :]]
                return True
        return False

    while over_cap():
        if any(marks.get(f.path) for f in ranked):
            dropped["stale"] += sum(1 for f in ranked if marks.get(f.path))
            marks = {}
            files = file_lines()
            sections = [
                ("files", [_FILES_HEADER, *files]) if key == "files" else (key, lines)
                for key, lines in sections
            ]
            continue
        # Lowest value first; the file list is trimmed last and from the bottom, so the
        # edited files at the top survive longest.
        if (
            drop_section("commands")
            or drop_section("folds")
            or drop_section("constraints")
            or drop_section("todos")
        ):
            continue
        if trim_file():
            continue
        break
    return "\n".join(assemble()), dropped


def _range_suffix(fact: FileFact) -> str:
    """Up to three ``Lstart-end`` labels from the fact's recorded ranges."""
    seen: set[tuple[int, int]] = set()
    labels: list[str] = []
    for start, end, _sha, _kind in fact.ranges:
        if (start, end) in seen:
            continue
        seen.add((start, end))
        labels.append(f"L{start}-{end}" if end > start else f"L{start}")
    return f" | {', '.join(labels[-MAX_RANGES_PER_FILE:])}" if labels else ""


def _render_v1(
    facts: SessionFacts,
    state_dir: Path,
    trigger: str,
    note: str | None = None,
    goal: str | None = None,
    stale: dict[str, str] | None = None,
) -> tuple[str, dict[str, int]]:
    ranked = sorted(facts.files, key=lambda f: -f.last_seen)[:MAX_FILES]
    marks = dict(stale or {})
    files = [_file_line(f, marks.get(f.path, "")) for f in ranked]
    folds = [
        _fold_line(f) for f in list(reversed(facts.folds)) if _obs_exists(state_dir, f.obs_id)
    ][:MAX_FOLDS]
    commands = [_command_line(c) for c in list(reversed(facts.commands))[:MAX_COMMANDS]]
    todos = [_todo_line(s, t) for s, t in _open_todos(facts.todos)[:MAX_TODOS]]

    sections: list[tuple[str, list[str]]] = []
    if files:
        sections.append(("files", [_FILES_HEADER, *files]))
    if folds:
        sections.append(("folds", [_FOLDS_HEADER, *folds]))
    if commands:
        sections.append(("commands", [_COMMANDS_HEADER, *commands]))
    if todos:
        sections.append(("todos", [_TODOS_HEADER, *todos]))

    header = [f'<context-fabric-carry v="0" trigger="{trigger}">']
    if goal:
        header.append(f"{_GOAL_LABEL} {goal}")
    header.append(_PROLOGUE)
    if note is not None:
        header.append(note)
    if facts.test is not None and trigger == "checkpoint":
        header.append(_test_line(facts.test))

    dropped = {"stale": 0, "commands": 0, "folds": 0, "todos": 0, "files": 0}

    def assemble() -> list[str]:
        return [*header, *(line for _, lines in sections for line in lines), _CLOSE]

    def over_cap() -> bool:
        return len("\n".join(assemble()).encode("utf-8")) > RENDER_CAP_BYTES

    def drop_section(name: str) -> bool:
        nonlocal sections
        for index, (key, lines) in enumerate(sections):
            if key == name:
                dropped[key] += len(lines) - 1
                sections = [*sections[:index], *sections[index + 1 :]]
                return True
        return False

    while over_cap():
        # Lowest priority first: cosmetic stale suffixes, then commands, folds,
        # todos, and finally files.
        if any(marks.get(f.path) for f in ranked):
            dropped["stale"] += sum(1 for f in ranked if marks.get(f.path))
            marks = {}
            files = [_file_line(f) for f in ranked]
            sections = [
                ("files", [_FILES_HEADER, *files]) if key == "files" else (key, lines)
                for key, lines in sections
            ]
            continue
        if drop_section("commands") or drop_section("folds"):
            continue
        if drop_section("todos") or drop_section("files"):
            continue
        break
    return "\n".join(assemble()), dropped


def _test_line(test: TestFact) -> str:
    status = (
        "passed" if test.ok is True else "failed" if test.ok is False else "ran, result unknown"
    )
    return f"{_TEST_LABEL} {_cap(_sanitize(test.key), 40)}, {status} (at {_clock(test.at)})"


def _clock(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%H:%M")


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
        ensure_dir(state_dir)
        with _receipt_lock:
            with open_log(state_dir / CARRY_LOG) as fh:
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
