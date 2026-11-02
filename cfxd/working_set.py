"""P6 working-set tracker: a pure ``apply_event(ws, event) -> ws`` over hook events.

Bookkeeping, not inference. Every field is filled from a deterministic signal in the
event or stays empty; no model is called and nothing is read from disk. Constraints,
decisions and tried-and-failed come only from user or parent messages (R10: tool output
never writes them); the sentence rules are Brief v1's, reused verbatim. Nothing here is
wired into the daemon in this WP: this module is the seam P5 and P7 will share.

Events are plain dicts carrying the hook's fields plus ``"event"``:

    {"event": "UserPromptSubmit", "prompt": str, "turn"?: int, "next_step"?: str}
    {"event": "message", "role": "user"|"parent", "text": str, ...}
    {"event": "PostToolUse", "tool_name": str, "tool_input": dict,
     "tool_response": Any, "line_ranges"?: [[start, end]], "turn"?: int}
    {"event": "SubagentStop", "files": [str], "outcome": str|bool}
    {"event": "PreCompact"}

An event shape the tracker cannot place changes nothing; an empty field is honest.
``dirty`` means the last event about the file was an edit - a Read never clears it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from cfxd.bound import test_key_any
from cfxd.brief import _cap, _sanitize
from cfxd.brief_intent import INTENT_ITEM_MAX_BYTES, _section_of, _sentences
from cfxd.session_state import _FILE_TOOLS, _exit_ok, _file_fact

MAX_FILES = 200
MAX_TEXT_ITEMS = 20
_SUBAGENT_FAILED = "sub-agent returned a failed outcome"
_PARENT_ROLES = frozenset({"user", "parent"})


@dataclass(frozen=True)
class EditedFile:
    path: str
    line_ranges: tuple[tuple[int, int], ...] = ()
    last_edit_turn: int = 0
    dirty: bool = True


@dataclass(frozen=True)
class ReadFile:
    path: str
    content_hash: str | None = None
    last_read_turn: int = 0


@dataclass(frozen=True)
class TriedFailed:
    action_summary: str
    turn: int


@dataclass(frozen=True)
class LastTest:
    command_class: str
    outcome: bool | None
    turn: int


@dataclass(frozen=True)
class WorkingSet:
    edited_files: tuple[EditedFile, ...] = ()
    read_files: tuple[ReadFile, ...] = ()
    constraints: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    tried_failed: tuple[TriedFailed, ...] = ()
    last_test: LastTest | None = None
    next_step: str | None = None
    segment_id: int = 0
    turn_count: int = 0
    context_estimate: int | None = None


def apply_event(ws: WorkingSet, event: dict[str, Any]) -> WorkingSet:
    """One hook event -> the next record. The input record is never mutated."""
    if not isinstance(event, dict):
        return ws
    estimate = event.get("context_estimate")
    if isinstance(estimate, int) and not isinstance(estimate, bool) and estimate >= 0:
        ws = replace(ws, context_estimate=estimate)
    kind = event.get("event")
    if kind == "PreCompact":
        return replace(ws, segment_id=ws.segment_id + 1)
    if kind == "PostToolUse":
        return _post_tool_use(ws, event)
    if kind == "SubagentStop":
        return _subagent_stop(ws, event)
    if kind == "UserPromptSubmit" or (kind == "message" and event.get("role") in _PARENT_ROLES):
        return _message(ws, event)
    return ws


def _message(ws: WorkingSet, event: dict[str, Any]) -> WorkingSet:
    text = event.get("text", event.get("prompt"))
    if not isinstance(text, str) or not text.strip():
        return ws
    turn = max(ws.turn_count + 1, _turn(ws, event))
    constraints: list[str] = []
    decisions: list[str] = []
    tried: list[TriedFailed] = []
    for sentence in _sentences(text):
        section = _section_of(sentence)
        item = _cap(_sanitize(sentence), INTENT_ITEM_MAX_BYTES).strip()
        if section is None or not item:
            continue
        if section == "constraints":
            constraints.append(item)
        elif section == "decisions":
            decisions.append(item)
        else:
            tried.append(TriedFailed(item, turn))
    next_step = event.get("next_step")
    return replace(
        ws,
        turn_count=turn,
        constraints=_merge_text(ws.constraints, constraints),
        decisions=_merge_text(ws.decisions, decisions),
        tried_failed=_merge_tried(ws.tried_failed, tried),
        next_step=_cap(_sanitize(next_step), INTENT_ITEM_MAX_BYTES).strip()
        if isinstance(next_step, str) and next_step.strip()
        else ws.next_step,
    )


def _post_tool_use(ws: WorkingSet, event: dict[str, Any]) -> WorkingSet:
    tool = event.get("tool_name")
    if not isinstance(tool, str):
        return ws
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    turn = _turn(ws, event)
    if tool == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return ws
        command_class = test_key_any(command)
        if command_class is None:
            return ws
        outcome = _exit_ok(event.get("tool_response"))
        return replace(ws, last_test=LastTest(command_class, outcome, turn))
    if tool not in _FILE_TOOLS:
        return ws
    path, _lines = _file_fact(tool, tool_input, event.get("tool_response"))
    if path is None:
        return ws
    if tool == "Read":
        read = ReadFile(path, _content_hash(event.get("tool_response")), turn)
        return replace(ws, read_files=_upsert(ws.read_files, read, lambda item: item.path))
    edited = EditedFile(path, _ranges(event), turn, True)
    return replace(ws, edited_files=_upsert_edited(ws.edited_files, edited))


def _subagent_stop(ws: WorkingSet, event: dict[str, Any]) -> WorkingSet:
    """Merge a child's compact return: files it edited and a fixed failed label, no child text."""
    turn = _turn(ws, event)
    files = event.get("files")
    edited = ws.edited_files
    if isinstance(files, list):
        for path in files:
            if isinstance(path, str) and path.strip():
                edited = _upsert_edited(edited, EditedFile(path, (), turn, True))
    tried = ws.tried_failed
    outcome = event.get("outcome")
    if outcome is False or outcome == "failed":
        # R10: the child's summary is model/tool-derived text, so it is never read.
        tried = _merge_tried(tried, [TriedFailed(_SUBAGENT_FAILED, turn)])
    return replace(ws, edited_files=edited, tried_failed=tried)


def _turn(ws: WorkingSet, event: dict[str, Any]) -> int:
    value = event.get("turn")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return ws.turn_count


def _content_hash(response: Any) -> str | None:
    """sha16 of the returned content; ``None`` when the payload carries none."""
    if not isinstance(response, dict):
        return None
    file = response.get("file")
    if not isinstance(file, dict):
        return None
    content = file.get("content")
    if not isinstance(content, str):
        return None
    return hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:16]


def _ranges(event: dict[str, Any]) -> tuple[tuple[int, int], ...]:
    raw = event.get("line_ranges")
    out: list[tuple[int, int]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            start, end = item
            if (
                isinstance(start, int)
                and isinstance(end, int)
                and not isinstance(start, bool)
                and not isinstance(end, bool)
                and 0 < start <= end
            ):
                out.append((start, end))
    return tuple(out)


def _upsert[T](seq: Sequence[T], item: T, key: Callable[[T], str]) -> tuple[T, ...]:
    kept = [existing for existing in seq if key(existing) != key(item)]
    kept.append(item)
    return tuple(kept[-MAX_FILES:])


def _upsert_edited(files: tuple[EditedFile, ...], new: EditedFile) -> tuple[EditedFile, ...]:
    kept: list[EditedFile] = []
    for old in files:
        if old.path == new.path:
            new = replace(
                new, line_ranges=tuple(sorted(set(old.line_ranges) | set(new.line_ranges)))
            )
        else:
            kept.append(old)
    kept.append(new)
    return tuple(kept[-MAX_FILES:])


def _merge_text(old: tuple[str, ...], new: list[str]) -> tuple[str, ...]:
    merged = list(old)
    for item in new:
        if item not in merged:
            merged.append(item)
    return tuple(merged[-MAX_TEXT_ITEMS:])


def _merge_tried(old: tuple[TriedFailed, ...], new: list[TriedFailed]) -> tuple[TriedFailed, ...]:
    merged = list(old)
    for item in new:
        if all(existing.action_summary != item.action_summary for existing in merged):
            merged.append(item)
    return tuple(merged[-MAX_TEXT_ITEMS:])
