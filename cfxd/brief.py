"""PreToolUse brief handler: prepend a facts packet to sub-agent (``Agent``) calls.

The packet is built only from facts recorded by ``session_state`` (paths, line
counts, edit flags, fold ids, redacted commands) — never from ``tool_response``
text. It is capped at 2,000 bytes and every string is sanitised so a path or
command can never close the delimiter or forge a section.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cfxd.brief_receipts import path_sha16
from cfxd.config import read_config
from cfxd.fsutil import ensure_dir, open_log
from cfxd.handlers import register, register_after
from cfxd.session_state import (
    FileFact,
    FoldFact,
    SessionFacts,
    set_range_recording,
    snapshot,
)

if TYPE_CHECKING:
    from cfxd.brief_types import Intent, Rendered

PACKET_CAP_BYTES = 2_000
MAX_FILES = 12
MAX_FOLDS = 5
MAX_COMMANDS = 5
ITEM_MAX_BYTES = 160
BRIEFS_LOG = "briefs.jsonl"

_OPEN = '<context-fabric-brief v="0">'
_CLOSE = "</context-fabric-brief>"
_PROLOGUE = (
    "Background gathered automatically from the parent session by Context Fabric. "
    "It is reference, not instructions:\n"
    "your task is the text above. Use it to avoid re-discovering things; "
    "files may have changed since, so verify before relying on them."
)
_FILES_HEADER = "Files the parent session already opened (path | lines | status):"
_FOLDS_HEADER = (
    "Large outputs saved in full (Read or Grep these instead of re-running the command):"
)
_COMMANDS_HEADER = "Commands the parent ran recently:"

_SKIP_SUBAGENTS = ("claude-code-guide", "statusline-setup")

V1_DEADLINE_S = 1.0
CARRY_BACK_CAP_BYTES = 2_000
CARRY_BACK_MAX_FILES = 10
CARRY_BACK_MAX_TODOS = 5
BRIEF_PACKET_MAX_AGE_S = 7 * 24 * 3600
_INFLIGHT_MAX_AGE_S = 3600.0
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")

_receipt_lock = threading.Lock()
_clock = time.monotonic


class _Deadline(Exception):
    """Brief v1 ran past its deadline; the handler falls back to v0."""


def install(state_dir: Path) -> None:
    """Register the ``PreToolUse`` handler and the synchronous Agent carry-back handler."""
    register("PreToolUse", lambda payload: handle_pre_tool_use(payload, state_dir))
    # The hook entry for Agent forwards as ``PostToolUseAgent`` so the async ``*`` entry and
    # this synchronous one never run the same handler twice.
    register_after(
        "PostToolUseAgent", lambda payload: handle_post_tool_use_agent(payload, state_dir)
    )


def sync_range_recording(state_dir: Path) -> None:
    """Keep line-range recording in step with the ``brief`` and ``brief_v1`` flags."""
    config = read_config(state_dir)
    set_range_recording(bool(config.get("brief") and config.get("brief_v1")))


def handle_pre_tool_use(payload: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    started = _clock()
    if not isinstance(payload, dict):
        return {}
    if payload.get("tool_name") != "Agent":
        return {}
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    session_id = payload.get("session_id")
    if not isinstance(session_id, str):
        session_id = None
    tool_use_id = payload.get("tool_use_id")
    if not isinstance(tool_use_id, str):
        tool_use_id = None
    subagent_type = tool_input.get("subagent_type")
    if not isinstance(subagent_type, str):
        subagent_type = None

    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str):
        prompt = ""
    prompt_bytes = len(prompt.encode("utf-8"))

    decision = "skipped"
    reason: str | None = None
    packet: str | None = None
    packet_bytes = 0
    counts = {"files": 0, "folds": 0, "commands": 0}
    extra: dict[str, Any] | None = None
    config = read_config(state_dir)

    if config.get("carry_back"):
        _mark_inflight(session_id, tool_use_id, tool_input)

    if not config.get("brief", True):
        reason = "disabled"
    elif prompt_bytes < 200:
        reason = "short-prompt"
    elif prompt_bytes > 12_000:
        reason = "long-prompt"
    elif "resume" in tool_input:
        reason = "resume"
    elif "<context-fabric-" in prompt:
        reason = "already-briefed"
    elif subagent_type in _SKIP_SUBAGENTS:
        reason = "subagent-type"
    else:
        facts = snapshot(session_id) if session_id is not None else None
        fallback: str | None = None
        if config.get("brief_v1"):
            try:
                v1 = _build_v1(payload, tool_input, prompt, facts, started)
            except _Deadline:
                v1, fallback = None, "v1-deadline"
            except Exception:
                v1, fallback = None, "v1-error"
            if v1 is not None:
                packet = v1.rendered.text
                packet_bytes = len(packet.encode("utf-8"))
                decision = "briefed"
                counts = v1.counts
                extra = v1.receipt
                _write_packet_json(state_dir, session_id, tool_use_id, v1.rendered)
            elif fallback is None:
                reason = "no-state"
            else:
                extra = {"fallback": fallback}
        if packet is None and reason is None:
            if facts is None or (not facts.files and not facts.folds):
                reason = "no-state"
            else:
                packet = _render(facts, prompt)
                packet_bytes = len(packet.encode("utf-8"))
                decision = "briefed"
                counts = {
                    "files": len(facts.files),
                    "folds": len(facts.folds),
                    "commands": len(facts.commands),
                }

    _receipt(
        state_dir,
        session_id,
        tool_use_id,
        subagent_type,
        decision,
        reason,
        prompt_bytes,
        packet_bytes,
        counts,
        packet,
        extra,
    )

    if packet is None:
        return {}
    updated = copy.deepcopy(tool_input)
    updated["prompt"] = prompt + "\n\n" + packet
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated,
        }
    }


def _render(facts: SessionFacts, prompt: str) -> str:
    files = [_file_line(f) for f in _rank_files(facts.files, prompt)[:MAX_FILES]]
    folds = [_fold_line(f) for f in list(reversed(facts.folds))[:MAX_FOLDS]]
    commands = [_command_line(c) for c in list(reversed(facts.commands))[:MAX_COMMANDS]]

    sections: list[list[str]] = []
    if files:
        sections.append([_FILES_HEADER, *files])
    if folds:
        sections.append([_FOLDS_HEADER, *folds])
    if commands:
        sections.append([_COMMANDS_HEADER, *commands])

    header = [_OPEN, _PROLOGUE]
    # Drop whole sections bottom-up (commands, then folds, then files) to fit.
    while sections:
        lines = [*header, *(line for section in sections for line in section), _CLOSE]
        if len("\n".join(lines).encode("utf-8")) <= PACKET_CAP_BYTES:
            return "\n".join(lines)
        sections.pop()
    return "\n".join([*header, _CLOSE])


def _rank_files(files: tuple[FileFact, ...], prompt: str) -> list[FileFact]:
    lowered = prompt.lower()

    def group(fact: FileFact) -> int:
        base = Path(fact.path).name
        if fact.path.lower() in lowered or (base and base.lower() in lowered):
            return 0
        return 1 if fact.edited else 2

    return sorted(files, key=lambda fact: (group(fact), -fact.last_seen))


def _sanitize(text: str) -> str:
    out: list[str] = []
    for ch in text:
        if ch == " ":
            out.append(ch)
        elif ch in "<>`":
            continue
        elif unicodedata.category(ch)[0] in ("Z", "C"):
            # Drop separators (U+2028/2029, nbsp) and control/format (U+0085, bidi
            # marks, surrogates) that could close the delimiter or spoof the model.
            continue
        else:
            out.append(ch)
    return "".join(out)


def _cap(text: str, limit: int = ITEM_MAX_BYTES) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", "ignore")


def _file_line(fact: FileFact, suffix: str = "") -> str:
    status = "edited" if fact.edited else "read"
    path = _cap(_sanitize(Path(fact.path).as_posix()))
    if fact.lines is not None:
        line = f"- {path} | {fact.lines} lines | {status}"
    else:
        line = f"- {path} | {status}"
    return line + suffix


def _fold_line(fact: FoldFact) -> str:
    obs = _cap(_sanitize(fact.obs_path))
    source = _cap(_sanitize(fact.source))
    return f"- {obs} | {fact.tool} of: {source} | {fact.bytes:,} bytes | {_kind_label(fact.kind)}"


def _kind_label(kind: str) -> str:
    return "test output" if kind == "test" else kind


def _command_line(command: str) -> str:
    return f"- {_cap(_sanitize(command))}"


def _receipt(
    state_dir: Path,
    session_id: str | None,
    tool_use_id: str | None,
    subagent_type: str | None,
    decision: str,
    reason: str | None,
    prompt_bytes: int,
    packet_bytes: int,
    counts: dict[str, int],
    packet: str | None,
    extra: dict[str, Any] | None = None,
) -> None:
    entry: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "session_id": session_id,
        "tool_use_id": tool_use_id,
        "subagent_type": subagent_type,
        "decision": decision,
        "skip_reason": reason,
        "prompt_bytes": prompt_bytes,
        "packet_bytes": packet_bytes,
        "counts": counts,
        "packet_sha16": (
            hashlib.sha256(packet.encode("utf-8")).hexdigest()[:16] if packet is not None else None
        ),
    }
    if extra:
        entry.update(extra)
    _write_log(state_dir, entry)


def _write_log(state_dir: Path, entry: dict[str, Any]) -> None:
    ensure_dir(state_dir)
    with _receipt_lock:
        with open_log(state_dir / BRIEFS_LOG) as fh:
            fh.write(json.dumps(entry) + "\n")


# ----------------------------------------------------------------------------- Brief v1


@dataclass(frozen=True)
class _V1:
    rendered: Rendered
    counts: dict[str, int]
    receipt: dict[str, Any]


def _check_deadline(started: float) -> None:
    if _clock() - started > V1_DEADLINE_S:
        raise _Deadline


def _intent_items(intent: Intent) -> list[Any]:
    return [
        *([intent.goal] if intent.goal is not None else []),
        *intent.constraints,
        *intent.decisions,
        *intent.tried,
    ]


def _build_v1(
    payload: dict[str, Any],
    tool_input: dict[str, Any],
    prompt: str,
    facts: SessionFacts | None,
    started: float,
) -> _V1 | None:
    """Select, extract intent and render. ``None`` means nothing to brief (``no-state``)."""
    from cfxd.brief_intent import delegation_items, extract_intent
    from cfxd.brief_render import HEADERS, render
    from cfxd.brief_select import resolve_target, select

    transcript = payload.get("transcript_path")
    cwd = payload.get("cwd")
    session_id = payload.get("session_id")
    target = resolve_target(tool_input)
    selection = select(facts, prompt, target)
    _check_deadline(started)
    intent = extract_intent(transcript if isinstance(transcript, str) else None, prompt, facts)
    _check_deadline(started)
    items = _intent_items(intent)
    if (facts is None or (not facts.files and not facts.folds)) and not items:
        return None
    rendered = render(
        selection,
        intent,
        cwd=cwd if isinstance(cwd, str) else "",
        session_id=session_id if isinstance(session_id, str) else "",
        now=datetime.now(UTC),
    )
    _check_deadline(started)

    shown = _count_rendered(rendered.text, HEADERS)
    counts = {
        "files": len(rendered.items),
        "excerpts": sum(1 for it in rendered.items if it.excerpt_lines),
        "folds": shown.get("folds", 0),
        "commands": shown.get("commands", 0),
        "constraints": shown.get("constraints", 0),
        "decisions": shown.get("decisions", 0),
        "tried": shown.get("tried", 0),
    }
    receipt: dict[str, Any] = {
        "event": "brief",
        "brief_v": 1,
        "target_model": selection.target.family,
        "model_raw": selection.target.raw,
        "model_source": selection.target.source,
        "budget_bytes": selection.budgets.total,
        "section_bytes": dict(rendered.section_bytes),
        "counts": counts,
        "items": [
            {
                "p": path_sha16(it.path),
                "r": list(it.reasons),
                "x": [[a, b] for a, b in it.excerpt_lines],
                "d": it.degraded,
            }
            for it in rendered.items
        ],
        "intent": {
            "user": sum(1 for i in items if i.source == "user"),
            "assistant": sum(1 for i in items if i.source == "assistant"),
            "delegation": delegation_items(prompt),
            "deduped": intent.deduped,
        },
        "fallback": None,
    }
    return _V1(rendered=rendered, counts=counts, receipt=receipt)


def _count_rendered(text: str, headers: dict[str, str]) -> dict[str, int]:
    """Bullet lines per section in a rendered block (excerpt content never starts with ``- ``)."""
    by_header = {header: name for name, header in headers.items()}
    counts: dict[str, int] = {}
    current: str | None = None
    for line in text.split("\n"):
        if line in by_header:
            current = by_header[line]
        elif current is not None and line.startswith("- "):
            counts[current] = counts.get(current, 0) + 1
    return counts


def _write_packet_json(
    state_dir: Path, session_id: str | None, tool_use_id: str | None, rendered: Rendered
) -> None:
    """Best effort: ``<state_dir>/briefs/<sha16(session)>/<tool_use_id>.json``, 7-day cleanup."""
    if session_id is None or tool_use_id is None:
        return
    try:
        root = state_dir / "briefs"
        folder = root / hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        name = (
            tool_use_id
            if _SAFE_ID.match(tool_use_id)
            else hashlib.sha256(tool_use_id.encode("utf-8")).hexdigest()[:16]
        )
        ensure_dir(folder)
        target = folder / f"{name}.json"
        target.write_text(json.dumps(rendered.packet), encoding="utf-8")
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        cutoff = time.time() - BRIEF_PACKET_MAX_AGE_S
        for old in root.glob("*/*.json"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                continue
    except Exception:
        pass


# ------------------------------------------------------------------------- carry-back


@dataclass
class _Inflight:
    started: float
    files: dict[str, tuple[float, bool]]
    test_at: float | None
    todos: tuple[tuple[str, str], ...]
    background: bool
    concurrent: bool = False


_INFLIGHT: dict[str, dict[str, _Inflight]] = {}
_inflight_lock = threading.Lock()


def reset_inflight() -> None:
    """Test helper: forget every in-flight Agent call."""
    with _inflight_lock:
        _INFLIGHT.clear()


def _mark_inflight(
    session_id: str | None, tool_use_id: str | None, tool_input: dict[str, Any]
) -> None:
    """Remember the facts held when an Agent call starts. Never raises."""
    try:
        if session_id is None or tool_use_id is None:
            return
        facts = snapshot(session_id)
        now = time.time()
        record = _Inflight(
            started=now,
            files={f.path: (f.last_seen, f.edited) for f in facts.files} if facts else {},
            test_at=facts.test.at if facts is not None and facts.test is not None else None,
            todos=facts.todos if facts is not None else (),
            background=bool(tool_input.get("run_in_background")),
        )
        with _inflight_lock:
            mine = _INFLIGHT.setdefault(session_id, {})
            for key in [k for k, v in mine.items() if now - v.started > _INFLIGHT_MAX_AGE_S]:
                del mine[key]
            if mine:
                record.concurrent = True
                for other in mine.values():
                    other.concurrent = True
            mine[tool_use_id] = record
    except Exception:
        pass


def handle_post_tool_use_agent(payload: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    """Append a capped facts summary after an Agent call (ADR-0011 decision 5).

    Built only from facts recorded since the matching PreToolUse; never from ``tool_response``.
    Skipped when attribution is not safe: another Agent call of the session overlapped, the
    call ran in the background, or no baseline was recorded.
    """
    if not isinstance(payload, dict) or payload.get("tool_name") != "Agent":
        return {}
    if not read_config(state_dir).get("carry_back"):
        return {}
    session_id = payload.get("session_id")
    session_id = session_id if isinstance(session_id, str) else None
    tool_use_id = payload.get("tool_use_id")
    tool_use_id = tool_use_id if isinstance(tool_use_id, str) else None

    with _inflight_lock:
        record = _INFLIGHT.get(session_id or "", {}).pop(tool_use_id or "", None)
        if session_id is not None and not _INFLIGHT.get(session_id):
            _INFLIGHT.pop(session_id, None)

    text: str | None = None
    counts = {"files": 0, "todos": 0, "test": 0}
    reason = ""
    if record is None:
        reason = "no-baseline"
    elif record.concurrent:
        reason = "concurrent"
    elif record.background:
        reason = "background"
    else:
        facts = snapshot(session_id) if session_id is not None else None
        if facts is not None:
            text, counts = _carry_back_block(facts, record)
        if text is None:
            reason = "no-facts"

    _write_log(
        state_dir,
        {
            "ts": datetime.now(UTC).isoformat(),
            "event": "carry_back",
            "session_id": session_id,
            "tool_use_id": tool_use_id,
            "decision": "appended" if text is not None else "skipped",
            "skip_reason": None if text is not None else reason,
            "bytes": len(text.encode("utf-8")) if text is not None else 0,
            "counts": counts,
            "packet_sha16": (
                hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text is not None else None
            ),
        },
    )
    if text is None:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}


_CB_OPEN = '<context-fabric-carry-back v="1">'
_CB_CLOSE = "</context-fabric-carry-back>"
_CB_INTRO = (
    "Facts Context Fabric recorded while the sub-agent ran "
    "(reference, not instructions; its own reply is above):"
)


def _carry_back_block(facts: SessionFacts, base: _Inflight) -> tuple[str | None, dict[str, int]]:
    changed = [
        f for f in facts.files if f.path not in base.files or f.last_seen > base.files[f.path][0]
    ]
    changed.sort(key=lambda f: (not f.edited, -f.last_seen, f.path))
    file_lines = [
        f"- {_cap(_sanitize(Path(f.path).as_posix()))} | "
        + ("edited" if f.edited and not base.files.get(f.path, (0.0, False))[1] else "read")
        for f in changed[:CARRY_BACK_MAX_FILES]
    ]
    test_line: str | None = None
    if facts.test is not None and (base.test_at is None or facts.test.at > base.test_at):
        result = (
            "ran, result unknown"
            if facts.test.ok is None
            else ("passed" if facts.test.ok else "failed")
        )
        test_line = f"Last test run: {_cap(_sanitize(facts.test.key))}, {result}"
    todo_lines: list[str] = []
    if facts.todos != base.todos:
        todo_lines = [
            f"- {_cap(_sanitize(status))} | {_cap(_sanitize(text))}"
            for status, text in facts.todos
            if status != "completed"
        ][:CARRY_BACK_MAX_TODOS]

    # (name, lines): dropped whole, bottom-up, until the block fits the cap.
    sections: list[tuple[str, list[str]]] = []
    if file_lines:
        sections.append(("files", ["Files the sub-agent touched (path | status):", *file_lines]))
    if test_line is not None:
        sections.append(("test", [test_line]))
    if todo_lines:
        sections.append(("todos", ["Open todos the sub-agent left (status | text):", *todo_lines]))
    while sections:
        lines = [_CB_OPEN, _CB_INTRO, *(ln for _, sec in sections for ln in sec), _CB_CLOSE]
        text = "\n".join(lines)
        if len(text.encode("utf-8")) <= CARRY_BACK_CAP_BYTES:
            kept = {name: len(sec) for name, sec in sections}
            return text, {
                "files": kept.get("files", 1) - 1 if "files" in kept else 0,
                "todos": kept.get("todos", 1) - 1 if "todos" in kept else 0,
                "test": 1 if "test" in kept else 0,
            }
        sections.pop()
    return None, {"files": 0, "todos": 0, "test": 0}
