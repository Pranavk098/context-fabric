"""Claude Code transcripts behind the ``cfxd.logs`` interface.

Reuses ``cfxd.audit``'s discovery and bounded line reader and ``routing``'s token
mapping and per-message dedupe, so this adapter cannot drift from the report
code. The existing Claude reports keep their own scan (they need fields this
minimal interface does not carry), so nothing in them changes.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from cfxd.logs import Session, Turn

name = "claude"


def default_root() -> Path:
    """``~/.claude/projects``."""
    from cfxd.audit import DEFAULT_ROOT

    return DEFAULT_ROOT


def sessions(root: Path, cutoff: datetime | None = None) -> Iterator[Session]:
    """Yield one Session per Claude Code parent transcript under ``root``."""
    from cfxd.audit import _discover, _iter_objects
    from cfxd.routing import _SYNTHETIC_MODEL, _tokens
    from cfxd.usage_dedupe import MessageUsage

    parents, _subs = _discover(root, cutoff)
    for path in parents:
        dedupe = MessageUsage(_iter_objects(path))
        turns: list[Turn] = []
        for obj in _iter_objects(path):
            if obj.get("type") != "assistant":
                continue
            message = obj.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict) or not dedupe.count_here(message):
                continue
            tokens = _tokens(usage)
            model = message.get("model")
            turns.append(
                Turn(
                    usage={
                        "input": tokens["input"],
                        "cache_read": tokens["cache_read"],
                        "cache_write": tokens["cache_creation_5m"] + tokens["cache_creation_1h"],
                        "output": tokens["output"],
                    },
                    model=model
                    if isinstance(model, str) and model.strip() and model != _SYNTHETIC_MODEL
                    else None,
                    tool_calls=dedupe.tool_uses(message) or 0,
                )
            )
        yield Session(turns=turns)
