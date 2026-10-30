"""Shared Brief v1 types (ADR-0011). Copy verbatim; change only by amending ADR-0011."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cfxd.session_state import FoldFact, TestFact

SECTIONS: tuple[str, ...] = (
    "task",
    "constraints",
    "decisions",
    "tried",
    "files",
    "excerpts",
    "folds",
    "commands",
)
REASONS: tuple[str, ...] = (
    "prompt_path",
    "prompt_symbol",
    "prompt_basename",
    "prompt_module",
    "edited",
    "import_neighbour",
    "recent",
)
INTENT_SOURCES: tuple[str, ...] = ("delegation", "user", "assistant")

# (start_line, end_line, sha16, kind); lines are 1-based inclusive; kind is "read" or "edit".
Range = tuple[int, int, str, str]


@dataclass(frozen=True)
class TargetModel:
    family: str  # "haiku" | "sonnet" | "opus" | "fable" | "unknown"
    raw: str | None  # sanitised input value, at most 64 bytes
    source: str  # "model" | "subagent_type" | "default"
    budget_bytes: int


@dataclass(frozen=True)
class Budgets:
    total: int
    sections: tuple[tuple[str, int], ...]  # (section, bytes) in SECTIONS order
    max_files: int
    max_excerpts: int
    max_folds: int
    max_commands: int

    def of(self, section: str) -> int:
        return dict(self.sections)[section]


@dataclass(frozen=True)
class FileItem:
    path: str  # absolute, as in FileFact.path
    lines: int | None
    edited: bool
    last_seen: float
    reasons: tuple[str, ...]  # subset of REASONS, in REASONS order
    matched: tuple[str, ...]  # prompt terms that matched this file
    ranges: tuple[Range, ...]  # from FileFact.ranges; () when absent
    excerpt: bool  # B2 should try an excerpt for this file


@dataclass(frozen=True)
class Selection:
    target: TargetModel
    budgets: Budgets
    files: tuple[FileItem, ...]  # best first, at most budgets.max_files
    folds: tuple[FoldFact, ...]  # newest first, at most budgets.max_folds
    commands: tuple[str, ...]  # newest first, redacted, at most budgets.max_commands
    todos: tuple[tuple[str, str], ...]  # open only, for the packet JSON
    test: TestFact | None
    terms: tuple[str, ...]  # paths and symbols taken from the delegation prompt


@dataclass(frozen=True)
class IntentItem:
    text: str  # sanitised, at most INTENT_ITEM_MAX_BYTES
    source: str  # one of INTENT_SOURCES
    turn: int  # transcript record index; -1 for the delegation prompt or a fact


@dataclass(frozen=True)
class Intent:
    goal: IntentItem | None
    constraints: tuple[IntentItem, ...]
    decisions: tuple[IntentItem, ...]
    tried: tuple[IntentItem, ...]
    deduped: int  # items dropped because the delegation prompt already says them


@dataclass(frozen=True)
class ItemReceipt:
    path: str
    reasons: tuple[str, ...]
    excerpt_lines: tuple[tuple[int, int], ...]  # ranges actually rendered
    degraded: bool  # had ranges but the file changed or was unreadable


@dataclass(frozen=True)
class Rendered:
    text: str  # the <context-fabric-brief v="1"> block
    packet: dict[str, Any]  # packet v0 object; packet.validate() has no FAIL
    section_bytes: tuple[tuple[str, int], ...]  # SECTIONS order, rendered bytes
    items: tuple[ItemReceipt, ...]  # one per rendered file, in render order
    dropped: tuple[tuple[str, int], ...]  # (section, items dropped to fit)
