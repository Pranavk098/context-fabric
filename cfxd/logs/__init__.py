"""One log-reader interface over agent session logs.

``--source claude`` (the default) is the existing Claude Code reader behind
``cfxd.logs.claude``; ``--source codex`` is beta. A source yields ``Session``
objects of ``Turn``s carrying four-bucket usage, a model id and a tool-call
count. Counts only: nothing here carries a path, prompt, command or tool output.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

#: The four usage buckets every source maps onto, in report order.
BUCKETS = ("input", "cache_read", "cache_write", "output")


@dataclass(frozen=True, slots=True)
class Turn:
    """One turn's token usage, its model id and its tool-call count."""

    usage: dict[str, int]
    model: str | None = None
    tool_calls: int = 0


@dataclass(frozen=True, slots=True)
class Session:
    """One session as a list of turns (counts only)."""

    turns: list[Turn] = field(default_factory=list)


class LogSource(Protocol):
    """What a log source must provide."""

    name: str

    def default_root(self) -> Path: ...

    def sessions(self, root: Path, cutoff: datetime | None = None) -> Iterator[Session]:
        """Yield one Session per transcript under ``root`` (file-mtime ``cutoff``)."""
        ...


def get_source(name: str) -> LogSource:
    """The source named ``name``; modules are imported lazily to keep imports acyclic."""
    if name == "claude":
        from cfxd.logs import claude

        return claude
    if name == "codex":
        from cfxd.logs import codex

        return codex
    raise ValueError(f"unknown log source: {name}")
