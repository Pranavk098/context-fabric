"""Agent-neutral audit model: frozen records, the reader registry, and helpers.

Shared by the per-agent readers (Codex now, OpenCode next) so the report
aggregator stays a single, agent-agnostic piece of code. Everything here is
counts and sizes only — the records never carry paths, commands, prompts or
tool output.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: A tool name is kept only as a plain identifier; anything path-like, too long
#: or non-ASCII collapses to ``other`` so no prompt/command can leak.
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,48}$")

_SINCE_DELTAS = {"7d": timedelta(days=7), "30d": timedelta(days=30)}


@dataclass(frozen=True, slots=True)
class Usage:
    """One turn's token usage, normalised so ``input`` excludes cached input."""

    input_uncached: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    reasoning: int = 0


@dataclass(frozen=True, slots=True)
class Turn:
    """A single user turn: when it happened and the usage it consumed."""

    ts: str | None
    usage: Usage


@dataclass(frozen=True, slots=True)
class ToolResult:
    """One tool result reduced to its (sanitised) tool name and byte size."""

    tool: str
    bytes: int


@dataclass(frozen=True, slots=True)
class Session:
    """One agent session reduced to counts and sizes."""

    agent: str
    session_id: str
    is_subagent: bool
    turns: list[Turn] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    compactions: int = 0


Reader = Callable[[Path, datetime | None], Iterator[Session]]

#: Populated by ``cfxd.agents`` as each reader is imported.
READERS: dict[str, Reader] = {}


def default_root(agent: str) -> Path:
    """Default transcript root for an agent on this OS, honouring overrides."""
    if agent == "codex":
        return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
    if agent == "opencode":
        return _default_opencode_root()
    raise ValueError(f"unknown agent: {agent}")


def _default_opencode_root() -> Path:
    """Default OpenCode database path (``~/.local/share/opencode/opencode.db``)."""
    override = os.environ.get("OPENCODE_DB")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "opencode" / "opencode.db"
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidate = Path(local) / "opencode" / "opencode.db"
            if candidate.exists():
                return candidate
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def sanitize_tool_name(name: str) -> str:
    """Collapse non-identifier tool names to ``other`` (privacy rule)."""
    return name if _TOOL_NAME_RE.fullmatch(name) else "other"


def since_cutoff(since: str, now: datetime | None = None) -> datetime | None:
    """Map a ``--since`` window to a file-mtime cutoff (``None`` = all time)."""
    delta = _SINCE_DELTAS.get(since)
    if delta is None:
        return None
    return (now if now is not None else datetime.now(UTC)) - delta
