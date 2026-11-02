"""Content classes: every file cfxd writes declares what it may hold at rest (P3).

Counts-only files may hold numbers, ids, hashes and short single-line labels; bounded-prose
files (carry and checkpoint snapshots) may hold paths, todo lines and constraint sentences;
Brief packet files hold pointers, ranges and counts. Excerpt text, prompt text and tool-output
text are not a declared class, and ``obs/`` (the retired fold blob store) is forbidden.

``audit_state_dir`` returns one ``"<relative path>: <reason>"`` per file that carries more than
its class allows, so an empty list is a pass. The daemon runs it at startup and prints the count
only; ``scripts/state_audit.py`` remains the manual, deeper probe.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Caps choose the boundary between "a field" and "a blob of text".
COUNTS_MAX_CHARS = 256
PROSE_MAX_CHARS = 4_000  # one command line or path in a snapshot/packet
SNAPSHOT_MAX_BYTES = 64 * 1024  # carry.MAX_SNAPSHOT_FILE_BYTES
INDEX_MAX_BYTES = 16 * 1024  # carry.SNAPSHOT_CAP_BYTES: the packet JSON cap
SECRET_MAX_BYTES = 128
OBS_DIR = "obs"
FORBIDDEN = "forbidden"
UNDECLARED = "undeclared"


@dataclass(frozen=True)
class ContentClass:
    """A declared class: whole-file cap, per-string cap, and whether strings stay on one line."""

    name: str
    max_bytes: int | None
    max_chars: int | None  # None: strings are not inspected
    single_line: bool = True


COUNTS = ContentClass("counts", None, COUNTS_MAX_CHARS)
INDEX = ContentClass("index", INDEX_MAX_BYTES, PROSE_MAX_CHARS, single_line=False)
SNAPSHOT = ContentClass("snapshot", SNAPSHOT_MAX_BYTES, PROSE_MAX_CHARS, single_line=False)
SECRET = ContentClass("secret", SECRET_MAX_BYTES, SECRET_MAX_BYTES)
# Launcher stdout/stderr: not a data store, so only the size of the state dir bounds it.
OPAQUE = ContentClass("opaque", None, None, single_line=False)


def classify(rel: Path) -> ContentClass | str:
    """Declared class for a state-dir-relative path, or ``FORBIDDEN``/``UNDECLARED``."""
    parts = rel.parts
    if not parts:
        return UNDECLARED
    name = re.sub(r"\.\d+$", "", parts[-1])  # rotated log: events.jsonl.1 -> events.jsonl
    if parts[0] == OBS_DIR:
        return FORBIDDEN
    if name in ("token", ".obs-purged"):
        return SECRET
    if name in ("state.json", "config.json") or name.endswith(".jsonl"):
        return COUNTS
    if name == "python":
        return INDEX  # launch.py's interpreter-path cache
    if name == "daemon.log":
        return OPAQUE
    if len(parts) == 2 and parts[0] == "history":
        return COUNTS if name.startswith("week-") and name.endswith(".json") else UNDECLARED
    if len(parts) == 2 and parts[0] in ("carry", "checkpoint"):
        return SNAPSHOT if name.endswith(".json") else UNDECLARED
    if len(parts) == 3 and parts[0] == "briefs":
        return INDEX if name.endswith(".json") else UNDECLARED
    return UNDECLARED


def _transient(path: Path) -> bool:
    """Atomic-write temp files (``.state.*.tmp``) never hold content and may survive a crash."""
    return path.name.startswith(".") and path.name.endswith(".tmp")


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _values(path: Path, text: str) -> list[Any]:
    name = path.name
    try:
        if re.search(r"\.jsonl(?:\.\d+)?$", name):
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        if name.endswith(".json"):
            return [json.loads(text)]
    except ValueError:
        return [text]  # malformed JSON is raw text: the string rules still apply
    return [text.rstrip("\r\n")]


def _check(path: Path, klass: ContentClass) -> list[str]:
    try:
        if klass.max_bytes is not None and path.stat().st_size > klass.max_bytes:
            return ["oversized"]
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    reasons: set[str] = set()
    for value in _values(path, text):
        for item in _strings(value):
            if klass.single_line and ("\n" in item or "\r" in item):
                reasons.add("multiline-text")
            elif klass.max_chars is not None and len(item) > klass.max_chars:
                reasons.add("long-text")
    return sorted(reasons)


def audit_state_dir(state_dir: Path) -> list[str]:
    """Every file under ``state_dir`` that exceeds its declared class; empty means pass."""
    if not state_dir.is_dir():
        return []
    out: list[str] = []
    for path in sorted(state_dir.rglob("*")):
        if not path.is_file() or _transient(path):
            continue
        where = path.relative_to(state_dir).as_posix()
        klass = classify(path.relative_to(state_dir))
        if isinstance(klass, str):
            reason = "forbidden (retired obs blob store)" if klass == FORBIDDEN else "undeclared"
            out.append(f"{where}: {reason}")
        else:
            out.extend(f"{where}: {reason}" for reason in _check(path, klass))
    return out
