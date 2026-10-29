"""Experimental ``cfxd packet``: conformance checks for a Context Packet v0 file.

The packet is the JSON object ``cfxd/checkpoint.py`` writes on ``/checkpoint``.
This module is a hand-written checker for ``docs/specs/context-packet-v0.schema.json``;
it uses no third-party library. It never changes a packet and never writes to the
state directory: the render-size check uses a throwaway temp directory.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cfxd import carry, checkpoint
from cfxd.session_state import (
    MAX_COMMANDS,
    MAX_FILES,
    MAX_FOLDS,
    redact_command,
)

TRIGGER = "checkpoint"
FOLD_KINDS = frozenset({"code", "test", "other"})
TODO_STATUSES = frozenset({"pending", "in_progress"})
FOLD_ID_RE = re.compile(r"\A[0-9a-f]{16}\Z")
TODO_MAX_BYTES = carry.TODO_MAX_BYTES

REQUIRED: tuple[str, ...] = (
    "v",
    "sid",
    "created",
    "trigger",
    "files",
    "folds",
    "commands",
    "todos",
    "cwd",
    "saved_at",
    "armed",
)
TOP_LEVEL = frozenset({*REQUIRED, "note", "test"})
FILE_FIELDS = frozenset({"path", "lines", "edited", "t"})
FOLD_FIELDS = frozenset({"id", "tool", "src", "bytes", "kind"})
TODO_FIELDS = frozenset({"s", "t"})
TEST_FIELDS = frozenset({"key", "ok", "at"})

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


@dataclass(frozen=True)
class Check:
    """One named conformance check and its verdict."""

    name: str
    status: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "reason": self.reason}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _unknown(item: dict[str, Any], allowed: frozenset[str], where: str) -> list[str]:
    return [f"{where} has unknown field {key!r}" for key in item if key not in allowed]


def _schema_problems(obj: Any) -> list[str]:
    """Shape, required fields, types, enums and unknown keys (caps are separate)."""
    if not isinstance(obj, dict):
        return ["packet is not a JSON object"]
    problems: list[str] = []
    problems.extend(_unknown(obj, TOP_LEVEL, "packet"))
    for key in REQUIRED:
        if key not in obj:
            problems.append(f"missing required field {key!r}")
    if obj.get("v") != 0 or not _is_int(obj.get("v")):
        problems.append("v must be the integer 0")
    if not isinstance(obj.get("sid"), str) or not obj.get("sid"):
        problems.append("sid must be a non-empty string")
    if _timestamp(obj.get("created")) is None:
        problems.append("created must be an ISO 8601 timestamp")
    if obj.get("trigger") != TRIGGER:
        problems.append(f"trigger must be {TRIGGER!r}")
    if not isinstance(obj.get("cwd"), str) or not obj.get("cwd"):
        problems.append("cwd must be a non-empty string")
    if _timestamp(obj.get("saved_at")) is None:
        problems.append("saved_at must be an ISO 8601 timestamp")
    if not isinstance(obj.get("armed"), bool):
        problems.append("armed must be a boolean")
    if "note" in obj and not isinstance(obj["note"], str):
        problems.append("note must be a string")

    files = obj.get("files")
    if not isinstance(files, list):
        problems.append("files must be an array")
        files = []
    for index, item in enumerate(files):
        where = f"files[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{where} must be an object")
            continue
        problems.extend(_unknown(item, FILE_FIELDS, where))
        for key in FILE_FIELDS:
            if key not in item:
                problems.append(f"{where} is missing {key!r}")
        if not isinstance(item.get("path"), str):
            problems.append(f"{where}.path must be a string")
        if item.get("lines") is not None and not _is_int(item.get("lines")):
            problems.append(f"{where}.lines must be an integer or null")
        if not isinstance(item.get("edited"), bool):
            problems.append(f"{where}.edited must be a boolean")
        if not _is_number(item.get("t")):
            problems.append(f"{where}.t must be a number")

    folds = obj.get("folds")
    if not isinstance(folds, list):
        problems.append("folds must be an array")
        folds = []
    for index, item in enumerate(folds):
        where = f"folds[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{where} must be an object")
            continue
        problems.extend(_unknown(item, FOLD_FIELDS, where))
        for key in FOLD_FIELDS:
            if key not in item:
                problems.append(f"{where} is missing {key!r}")
        if not isinstance(item.get("id"), str) or FOLD_ID_RE.fullmatch(item.get("id", "")) is None:
            problems.append(f"{where}.id must be 16 lowercase hex characters")
        if not isinstance(item.get("tool"), str):
            problems.append(f"{where}.tool must be a string")
        if not isinstance(item.get("src"), str):
            problems.append(f"{where}.src must be a string")
        if not _is_int(item.get("bytes")) or item.get("bytes", -1) < 0:
            problems.append(f"{where}.bytes must be a non-negative integer")
        if item.get("kind") not in FOLD_KINDS:
            problems.append(f"{where}.kind must be one of code, test, other")

    commands = obj.get("commands")
    if not isinstance(commands, list):
        problems.append("commands must be an array")
        commands = []
    for index, value in enumerate(commands):
        if not isinstance(value, str):
            problems.append(f"commands[{index}] must be a string")

    todos = obj.get("todos")
    if not isinstance(todos, list):
        problems.append("todos must be an array")
        todos = []
    for index, item in enumerate(todos):
        where = f"todos[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{where} must be an object")
            continue
        problems.extend(_unknown(item, TODO_FIELDS, where))
        for key in TODO_FIELDS:
            if key not in item:
                problems.append(f"{where} is missing {key!r}")
        if item.get("s") not in TODO_STATUSES:
            problems.append(f"{where}.s must be pending or in_progress")
        if not isinstance(item.get("t"), str):
            problems.append(f"{where}.t must be a string")

    if "test" in obj:
        test = obj["test"]
        if not isinstance(test, dict):
            problems.append("test must be an object")
        else:
            problems.extend(_unknown(test, TEST_FIELDS, "test"))
            for key in TEST_FIELDS:
                if key not in test:
                    problems.append(f"test is missing {key!r}")
            if not isinstance(test.get("key"), str) or not test.get("key"):
                problems.append("test.key must be a non-empty string")
            if test.get("ok") is not None and not isinstance(test.get("ok"), bool):
                problems.append("test.ok must be a boolean or null")
            if not _is_number(test.get("at")):
                problems.append("test.at must be a number")
    return problems


def _cap_problems(obj: dict[str, Any]) -> list[str]:
    """The caps the writer enforces: array lengths, todo bytes, note characters."""
    problems: list[str] = []
    limits = (
        ("files", MAX_FILES),
        ("folds", MAX_FOLDS),
        ("commands", MAX_COMMANDS),
        ("todos", carry.MAX_TODOS),
    )
    for key, limit in limits:
        value = obj.get(key)
        if isinstance(value, list) and len(value) > limit:
            problems.append(f"{key} has {len(value)} entries (cap {limit})")
    todos = obj.get("todos")
    if isinstance(todos, list):
        for index, item in enumerate(todos):
            text = item.get("t") if isinstance(item, dict) else None
            if isinstance(text, str) and len(text.encode("utf-8")) > TODO_MAX_BYTES:
                problems.append(f"todos[{index}].t is over {TODO_MAX_BYTES} bytes")
    note = obj.get("note")
    if isinstance(note, str) and len(note) > checkpoint.NOTE_MAX_CHARS:
        problems.append(f"note is over {checkpoint.NOTE_MAX_CHARS} characters")
    snapshot = {
        key: value for key, value in obj.items() if key not in ("cwd", "saved_at", "armed", "note")
    }
    size = len(json.dumps(snapshot).encode("utf-8"))
    if size > carry.SNAPSHOT_CAP_BYTES:
        problems.append(f"snapshot is {size} bytes (cap {carry.SNAPSHOT_CAP_BYTES})")
    return problems


def _provenance_problems(obj: dict[str, Any]) -> list[str]:
    """Every entry must carry the fields that tie it to an observed event."""
    problems: list[str] = []
    files = obj.get("files")
    if isinstance(files, list):
        previous: float | None = None
        for index, item in enumerate(files):
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            if not isinstance(path, str) or not path.strip():
                problems.append(f"files[{index}] has no path to tie it to an event")
            if not isinstance(item.get("edited"), bool):
                problems.append(f"files[{index}] has no observed status")
            stamp = item.get("t")
            if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
                problems.append(f"files[{index}] has no event time")
            else:
                value = float(stamp)
                if previous is not None and value < previous:
                    problems.append("files are not in oldest-first order")
                previous = value
    commands = obj.get("commands")
    if isinstance(commands, list):
        for index, value in enumerate(commands):
            if not isinstance(value, str) or not value.strip():
                problems.append(f"commands[{index}] is not a recorded command")
    todos = obj.get("todos")
    if isinstance(todos, list):
        for index, item in enumerate(todos):
            if not isinstance(item, dict):
                continue
            if item.get("s") not in TODO_STATUSES:
                problems.append(f"todos[{index}] is not an open todo")
            text = item.get("t")
            if not isinstance(text, str) or not text.strip():
                problems.append(f"todos[{index}] has no todo text")
    folds = obj.get("folds")
    if isinstance(folds, list):
        for index, item in enumerate(folds):
            if isinstance(item, dict) and not str(item.get("src", "")).strip():
                problems.append(f"folds[{index}] has no observed source")
    test = obj.get("test")
    if isinstance(test, dict):
        if not isinstance(test.get("key"), str) or not test.get("key"):
            problems.append("test has no runner key")
        if not _is_number(test.get("at")):
            problems.append("test has no event time")
    return problems


def _size_check(obj: dict[str, Any], cwd: str | None) -> Check:
    """Render with the shipped renderer; the cap may only be met by dropping data."""
    saved = checkpoint._saved_at(obj.get("saved_at"))
    with tempfile.TemporaryDirectory(prefix="cfx-packet-") as raw:
        state_dir = Path(raw)
        facts = carry.facts_from_snapshot(obj, state_dir)
        if facts is None:
            return Check("size", FAIL, "the renderer cannot read this packet")
        if facts.folds:
            obs = state_dir / "obs"
            obs.mkdir(parents=True, exist_ok=True)
            for fold in facts.folds:
                try:
                    (obs / f"{fold.obs_id}.txt").touch()
                except OSError:
                    pass
        stale = checkpoint._stale_marks(obj, saved) if cwd is not None else None
        goal = obj.get("note") if isinstance(obj.get("note"), str) else None
        text, dropped = carry.render_report(
            facts,
            state_dir,
            TRIGGER,
            note=checkpoint._note(saved),
            goal=goal,
            stale=stale,
        )
    size = len(text.encode("utf-8"))
    lost = sum(count for name, count in dropped.items() if name != "stale")
    if size > carry.RENDER_CAP_BYTES:
        return Check("size", FAIL, f"render is {size} bytes (cap {carry.RENDER_CAP_BYTES})")
    if lost:
        return Check(
            "size",
            FAIL,
            f"render would drop {lost} line(s) to fit {carry.RENDER_CAP_BYTES} bytes",
        )
    return Check("size", PASS, f"render is {size} bytes (cap {carry.RENDER_CAP_BYTES})")


def _staleness_check(obj: dict[str, Any], cwd: str | None) -> Check:
    if cwd is None:
        return Check("staleness", SKIP, "no --cwd given; listed files were not stat'ed")
    files = obj.get("files")
    if not isinstance(files, list):
        return Check("staleness", SKIP, "packet has no file list")
    saved = checkpoint._saved_at(obj.get("saved_at"))
    cutoff = saved.timestamp() if saved is not None else None
    missing = 0
    changed = 0
    checked = 0
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        path = Path(item["path"])
        if not path.is_absolute():
            path = Path(cwd) / path
        checked += 1
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            missing += 1
            continue
        if cutoff is not None and mtime > cutoff:
            changed += 1
    if missing or changed:
        return Check(
            "staleness",
            FAIL,
            f"{checked} checked, {changed} changed, {missing} missing",
        )
    return Check("staleness", PASS, f"{checked} checked, 0 changed, 0 missing")


def _masking_check(obj: dict[str, Any]) -> Check:
    commands = obj.get("commands")
    if not isinstance(commands, list):
        return Check("masking", SKIP, "packet has no command list")
    for index, value in enumerate(commands):
        if not isinstance(value, str):
            continue
        once = redact_command(value)
        if once != value:
            return Check(
                "masking", FAIL, f"commands[{index}] still contains a secret-looking token"
            )
        if redact_command(once) != once:
            return Check("masking", FAIL, f"commands[{index}] is not stable under redaction")
    return Check("masking", PASS, f"{len(commands)} command(s) masked and idempotent")


def _walk_strings(value: Any, where: str) -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(where, value)]
    if isinstance(value, dict):
        found: list[tuple[str, str]] = []
        for key, item in value.items():
            found.append((f"{where}.<key>", str(key)))
            found.extend(_walk_strings(item, f"{where}.{key}"))
        return found
    if isinstance(value, list):
        found = []
        for index, item in enumerate(value):
            found.extend(_walk_strings(item, f"{where}[{index}]"))
        return found
    return []


def _characters_check(obj: dict[str, Any]) -> Check:
    bad = 0
    first = ""
    for where, text in _walk_strings(obj, "$"):
        for char in text:
            if char in "<>" or unicodedata.category(char) in ("Cc", "Cf", "Cs"):
                bad += 1
                if not first:
                    first = where
                break
    if bad:
        return Check(
            "characters",
            FAIL,
            f"{bad} string(s) contain a control character or angle bracket (first at {first})",
        )
    return Check("characters", PASS, "no control characters or angle brackets")


def validate(obj: Any, cwd: str | None = None) -> list[Check]:
    """Run every enabled check; ``staleness`` is skipped without ``cwd``."""
    if not isinstance(obj, dict):
        return [
            Check("schema", FAIL, "packet is not a JSON object"),
            Check("caps", SKIP, "not an object"),
            Check("size", SKIP, "not an object"),
            Check("provenance", SKIP, "not an object"),
            Check("staleness", SKIP, "not an object"),
            Check("masking", SKIP, "not an object"),
            Check("characters", SKIP, "not an object"),
        ]
    schema = _schema_problems(obj)
    caps = _cap_problems(obj)
    provenance = _provenance_problems(obj)
    masking = _masking_check(obj)
    characters = _characters_check(obj)
    return [
        Check("schema", FAIL if schema else PASS, "; ".join(schema[:3]) or "shape and enums hold"),
        Check(
            "caps",
            FAIL if caps else PASS,
            "; ".join(caps[:3]) or "within every array and text cap",
        ),
        _size_check(obj, cwd),
        Check(
            "provenance",
            FAIL if provenance else PASS,
            "; ".join(provenance[:3]) or "every entry has an observed source",
        ),
        _staleness_check(obj, cwd),
        masking,
        characters,
    ]


def passed(checks: list[Check]) -> bool:
    return all(check.status != FAIL for check in checks)


def load(path: Path) -> Any:
    """Parse one packet file; raises ``OSError`` or ``ValueError``."""
    return json.loads(path.read_text(encoding="utf-8"))
