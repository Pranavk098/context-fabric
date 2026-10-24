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
import threading
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cfxd.config import read_config
from cfxd.fsutil import ensure_dir, open_log
from cfxd.handlers import register
from cfxd.session_state import FileFact, FoldFact, SessionFacts, snapshot

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

_receipt_lock = threading.Lock()


def install(state_dir: Path) -> None:
    """Register the ``PreToolUse`` handler."""
    register("PreToolUse", lambda payload: handle_pre_tool_use(payload, state_dir))


def handle_pre_tool_use(payload: dict[str, Any], state_dir: Path) -> dict[str, Any]:
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

    if not read_config(state_dir).get("brief", True):
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


def _file_line(fact: FileFact) -> str:
    status = "edited" if fact.edited else "read"
    path = _cap(_sanitize(Path(fact.path).as_posix()))
    if fact.lines is not None:
        return f"- {path} | {fact.lines} lines | {status}"
    return f"- {path} | {status}"


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
) -> None:
    entry = {
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
    ensure_dir(state_dir)
    with _receipt_lock:
        with open_log(state_dir / BRIEFS_LOG) as fh:
            fh.write(json.dumps(entry) + "\n")
