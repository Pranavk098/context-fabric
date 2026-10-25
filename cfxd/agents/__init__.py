"""Agent-neutral transcript audit readers (Codex now, OpenCode next)."""

from __future__ import annotations

from cfxd.agents.codex import read_sessions as _read_codex
from cfxd.agents.model import (
    READERS,
    Session,
    ToolResult,
    Turn,
    Usage,
    default_root,
    sanitize_tool_name,
    since_cutoff,
)
from cfxd.agents.opencode import OpenCodeDBError
from cfxd.agents.opencode import read_sessions as _read_opencode
from cfxd.agents.report import build_report, format_human

READERS["codex"] = _read_codex
READERS["opencode"] = _read_opencode

__all__ = [
    "OpenCodeDBError",
    "READERS",
    "Session",
    "ToolResult",
    "Turn",
    "Usage",
    "build_report",
    "default_root",
    "format_human",
    "sanitize_tool_name",
    "since_cutoff",
]
