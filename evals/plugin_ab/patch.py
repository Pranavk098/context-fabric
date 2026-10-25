"""Apply a task patch to a working copy.

A patch maps a file path (relative to the working copy root) to a list of ops.
Supported ops:

- ``{"old": s, "new": t}``      replace the first occurrence (must appear once)
- ``{"old": s, "new": t, "all": true}``  replace every occurrence (>= 1)
- ``{"create": content}``       create a new file (must not exist)
- ``{"append": content}``       append text to the end of the file

The harness is stdlib-only; this applier replaces the ``patch`` binary so the
runner and the fake ``claude`` work identically on every OS.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def apply_patch(root: Path, patch: dict[str, Any]) -> None:
    for rel, ops in patch.items():
        if not isinstance(ops, list):
            raise ValueError(f"patch for {rel!r} must be a list of ops")
        path = root / rel
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        for op in ops:
            if not isinstance(op, dict):
                raise ValueError(f"op for {rel!r} must be an object")
            if "create" in op:
                if path.exists():
                    raise ValueError(f"cannot create existing file {rel!r}")
                text = str(op["create"])
                continue
            if "append" in op:
                text += str(op["append"])
                continue
            old = op.get("old")
            new = op.get("new")
            if not isinstance(old, str) or not isinstance(new, str):
                raise ValueError(f"replace op for {rel!r} needs string old/new")
            if op.get("all"):
                if old not in text:
                    raise ValueError(f"replace-all pattern not found in {rel!r}")
                text = text.replace(old, new)
            else:
                count = text.count(old)
                if count != 1:
                    raise ValueError(f"expected 1 occurrence of {old!r} in {rel!r}, found {count}")
                text = text.replace(old, new, 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
