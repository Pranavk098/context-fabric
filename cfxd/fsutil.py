"""Filesystem helpers: create state dirs and log files with restrictive POSIX modes.

Shared by the daemon, its handlers, and the launcher so that state dirs are 0700
and log files 0600 regardless of the ambient umask. On Windows ``os.chmod`` is a
best-effort no-op; callers still get the same directory/file semantics.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any


def ensure_dir(path: Path) -> None:
    """Create ``path`` as 0700, or chmod an existing one to 0700 (best effort)."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def open_log(path: Path) -> IO[str]:
    """Open ``path`` for append, creating it 0600 and tightening an existing file."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return os.fdopen(fd, "a", encoding="utf-8")


def iter_json_lines(fh: IO[Any], max_bytes: int, max_total: int | None = None) -> Iterator[str]:
    """Yield decoded lines, skipping over-long ones without buffering them whole.

    ``readline`` is capped at ``max_bytes + 1``, so an over-long line (a planted
    multi-GB JSONL line) is drained in bounded chunks instead of being read into
    memory. ``max_total`` stops after that many units read (bytes for binary
    streams, characters for the decompressed text streams of ``.zst`` files).
    """
    total = 0
    while True:
        raw = fh.readline(max_bytes + 1)
        if not raw:
            return
        total += len(raw)
        if max_total is not None and total > max_total:
            return
        newline: Any = b"\n" if isinstance(raw, bytes) else "\n"
        if len(raw) > max_bytes:
            while raw and not raw.endswith(newline):
                raw = fh.readline(max_bytes)
            continue
        yield raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw


def rotate(path: Path, max_bytes: int) -> None:
    """Move ``path`` to ``path.1`` (dropping the old one) once it exceeds ``max_bytes``."""
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            backup = path.with_name(path.name + ".1")
            backup.unlink(missing_ok=True)
            os.replace(path, backup)
    except OSError:
        pass
