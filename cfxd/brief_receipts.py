"""Brief receipts: join ``briefs.jsonl`` entries to the sub-agent transcripts they briefed.

For every brief the PreToolUse hook wrote (``cfxd/brief.py``), compute what the packet
cost and what the child did with it: packet bytes and items, the child's model, which
briefed files the child re-read anyway, which files it read that the parent had
already read but the packet left out, the child's deduplicated token use, its
compactions and context overflows, and the outcome.

Join key (strongest first; the method used is reported per receipt as ``join``):

1. ``tool_use_id`` -> ``agentId``. The hook records the ``Agent`` tool_use id. The
   parent transcript's matching ``tool_result`` line carries ``toolUseResult.agentId``,
   and the child transcript is ``<session-id>/subagents/agent-<agentId>.jsonl`` next to
   the parent's ``<session-id>.jsonl``. This is exact and does not depend on clocks.
2. ``packet_sha16``. The child's first user message is the parent's prompt plus the
   packet, so re-hashing the packet text found there reproduces the recorded
   ``packet_sha16``. Used when the parent line is missing or has no agentId. It only
   works for briefed entries (skipped ones have no packet), and identical packets in
   one session are ambiguous, so a sha match is accepted only when it is unique.
3. Timestamps are deliberately not used: parallel sub-agents start within
   milliseconds, so a time window would silently mis-attribute children.

A brief with no parent transcript, no agentId link, or no child file is returned with
``matched=False`` and a ``unmatched_reason``; nothing raises on missing or partial data.

Privacy: packets are built from session facts, and this module never keeps tool output
text. From transcripts it reads tool_use inputs (file paths), usage, model names,
line-level flags and the child's first user message (to recover the packet it was
given). The only tool_result text consulted is a regex for the ``agentId`` when the
structured ``toolUseResult`` is absent; the text itself is not stored.

Definitions: a "read" is a ``Read``/``NotebookRead`` tool_use. The parent's reads count
only if they occur before the ``Agent`` call in the parent's file. An "overflow" is a
child assistant message with ``stop_reason == "model_context_window_exceeded"`` or an
API-error line (``isApiErrorMessage``) saying the prompt is too long / context window
exceeded. A "compaction" is a line with ``isCompactSummary`` or
``subtype == "compact_boundary"``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from cfxd.usage_dedupe import MessageUsage

_READ_TOOLS = frozenset({"Read", "NotebookRead"})
_AGENT_TOOLS = frozenset({"Agent", "Task"})
_PACKET_RE = re.compile(r"<context-fabric-brief\b[^>]*>.*?</context-fabric-brief>", re.DOTALL)
_AGENT_ID_RE = re.compile(r"agentId:\s*([A-Za-z0-9_-]+)")
_OVERFLOW_RE = re.compile(r"prompt is too long|context (window|length)|too many tokens", re.I)
_TOKEN_FIELDS = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("cache_read", "cache_read_input_tokens"),
    ("cache_creation", "cache_creation_input_tokens"),
)


@dataclass
class BriefReceipt:
    """One brief joined to its child. JSON-serialisable via :meth:`to_dict`."""

    session_id: str | None
    tool_use_id: str | None
    subagent_type: str | None
    decision: str | None
    skip_reason: str | None
    ts: str | None
    matched: bool = False
    unmatched_reason: str | None = None
    join: str | None = None
    agent_id: str | None = None
    prompt_bytes: int = 0
    packet_bytes: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    packet_items: int | None = None
    target_model: str | None = None
    briefed_files: list[str] = field(default_factory=list)
    child_files_read: list[str] = field(default_factory=list)
    rereads_of_briefed: list[str] = field(default_factory=list)
    parent_read_not_briefed: list[str] = field(default_factory=list)
    child_tokens: dict[str, int] = field(default_factory=dict)
    child_turns: int = 0
    compactions: int = 0
    overflows: int = 0
    outcome: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def receipts_to_json(receipts: Iterable[BriefReceipt]) -> list[dict[str, Any]]:
    """JSON-serialisable form (a list of plain dicts)."""
    return [r.to_dict() for r in receipts]


def join_receipts(
    briefs_path: Path,
    projects_root: Path | None = None,
    parent_transcripts: dict[str, Path] | None = None,
) -> list[BriefReceipt]:
    """Join every entry of ``briefs_path`` (a ``briefs.jsonl``) to its child transcript.

    Parent transcripts are found as ``<projects_root>/*/<session_id>.jsonl`` (default
    ``~/.claude/projects``) unless ``parent_transcripts`` maps session id -> path.
    """
    root = projects_root if projects_root is not None else Path.home() / ".claude" / "projects"
    explicit = parent_transcripts or {}
    entries = [e for e in _iter_jsonl(briefs_path)]
    parents: dict[str, _Parent | None] = {}
    out: list[BriefReceipt] = []
    for entry in entries:
        sid = entry.get("session_id")
        sid = sid if isinstance(sid, str) and sid else None
        parent: _Parent | None = None
        if sid is not None:
            if sid not in parents:
                path = explicit.get(sid) or _find_parent(root, sid)
                parents[sid] = _scan_parent(path) if path is not None else None
            parent = parents[sid]
        out.append(_receipt(entry, sid, parent))
    return out


# --------------------------------------------------------------------- internals


@dataclass
class _Parent:
    path: Path
    reads: list[tuple[int, str]]  # (line index, path) for non-sidechain Read calls
    agent_calls: dict[str, int]  # Agent tool_use id -> line index
    agent_ids: dict[str, str]  # Agent tool_use id -> agentId
    results: dict[str, tuple[str | None, bool]]  # tool_use id -> (status, is_error)
    _children: dict[str, _Child] | None = None

    def children(self) -> dict[str, _Child]:
        if self._children is None:
            self._children = {}
            sub = self.path.with_suffix("") / "subagents"
            if sub.is_dir():
                for f in sorted(sub.glob("agent-*.jsonl")):
                    child = _scan_child(f)
                    if child is not None:
                        self._children[child.agent_id] = child
        return self._children


@dataclass
class _Child:
    agent_id: str
    packet: str | None
    packet_sha16: str | None
    briefed: list[str]
    packet_items: int
    model: str | None
    reads: list[str]
    tokens: dict[str, int]
    turns: int
    compactions: int
    overflows: int


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


def _find_parent(root: Path, session_id: str) -> Path | None:
    if "/" in session_id or "\\" in session_id or not root.is_dir():
        return None
    for p in root.glob(f"*/{session_id}.jsonl"):
        return p
    return None


def _blocks(obj: dict[str, Any]) -> list[dict[str, Any]]:
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


def _read_path(block: dict[str, Any]) -> str | None:
    if block.get("type") != "tool_use" or block.get("name") not in _READ_TOOLS:
        return None
    inp = block.get("input")
    if not isinstance(inp, dict):
        return None
    p = inp.get("file_path") or inp.get("notebook_path")
    return p if isinstance(p, str) and p else None


def _scan_parent(path: Path) -> _Parent:
    parent = _Parent(path, [], {}, {}, {})
    for idx, obj in enumerate(_iter_jsonl(path)):
        if obj.get("isSidechain"):
            continue
        tur = obj.get("toolUseResult")
        for b in _blocks(obj):
            kind = b.get("type")
            if kind == "tool_use":
                rp = _read_path(b)
                if rp is not None:
                    parent.reads.append((idx, rp))
                tid = b.get("id")
                if b.get("name") in _AGENT_TOOLS and isinstance(tid, str):
                    parent.agent_calls[tid] = idx
            elif kind == "tool_result":
                tid = b.get("tool_use_id")
                if not isinstance(tid, str):
                    continue
                status = tur.get("status") if isinstance(tur, dict) else None
                parent.results[tid] = (
                    status if isinstance(status, str) else None,
                    bool(b.get("is_error")),
                )
                aid = tur.get("agentId") if isinstance(tur, dict) else None
                if not isinstance(aid, str):
                    aid = _agent_id_from_text(b.get("content"))
                if isinstance(aid, str) and aid:
                    parent.agent_ids[tid] = aid
    return parent


def _agent_id_from_text(content: Any) -> str | None:
    texts: list[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        texts.extend(
            c["text"] for c in content if isinstance(c, dict) and isinstance(c.get("text"), str)
        )
    for t in texts:
        m = _AGENT_ID_RE.search(t)
        if m:
            return m.group(1)
    return None


def _first_user_text(obj: dict[str, Any]) -> str:
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)
        )
    return ""


def _parse_packet(packet: str) -> tuple[list[str], int]:
    """Briefed file paths and total item count from a packet's bullet lines."""
    files: list[str] = []
    items = 0
    header = ""
    for line in packet.splitlines():
        if line.startswith("- "):
            items += 1
            if header.lower().startswith("files"):
                files.append(line[2:].split(" | ", 1)[0].strip())
        else:
            header = line.strip()
    return files, items


def _scan_child(path: Path) -> _Child | None:
    objs = list(_iter_jsonl(path))
    if not objs:
        return None
    agent_id = next((o["agentId"] for o in objs if isinstance(o.get("agentId"), str)), None)
    if agent_id is None:
        stem = path.stem
        agent_id = stem[len("agent-") :] if stem.startswith("agent-") else stem
    packet: str | None = None
    for o in objs:
        if o.get("type") == "user":
            m = _PACKET_RE.search(_first_user_text(o))
            packet = m.group(0) if m else None
            break
    briefed, items = _parse_packet(packet) if packet else ([], 0)
    dedupe = MessageUsage(objs)
    tokens = {name: 0 for name, _ in _TOKEN_FIELDS}
    turns = compactions = overflows = 0
    model: str | None = None
    reads: list[str] = []
    for o in objs:
        if o.get("isCompactSummary") or o.get("subtype") == "compact_boundary":
            compactions += 1
        msg = o.get("message")
        if o.get("type") != "assistant" or not isinstance(msg, dict):
            continue
        if msg.get("stop_reason") == "model_context_window_exceeded":
            overflows += 1
        elif o.get("isApiErrorMessage") and _OVERFLOW_RE.search(_first_user_text(o)):
            overflows += 1
        m = msg.get("model")
        if model is None and isinstance(m, str) and m and m != "<synthetic>":
            model = m
        for b in _blocks(o):
            rp = _read_path(b)
            if rp is not None:
                reads.append(rp)
        usage = msg.get("usage")
        if isinstance(usage, dict) and dedupe.count_here(msg):
            final = dedupe.final_usage(msg, usage)
            turns += 1
            for name, key in _TOKEN_FIELDS:
                v = final.get(key)
                if isinstance(v, int) and not isinstance(v, bool):
                    tokens[name] += v
    tokens["total"] = sum(tokens.values())
    sha = hashlib.sha256(packet.encode("utf-8")).hexdigest()[:16] if packet else None
    return _Child(
        agent_id, packet, sha, briefed, items, model, reads, tokens, turns, compactions, overflows
    )


def _norm(p: str) -> str:
    s = p.replace("\\", "/").strip()
    if len(s) > 1 and s[1] == ":":
        s = s[0].lower() + s[1:]
    return s


def _same(a: str, b: str) -> bool:
    """Same file: equal after slash normalisation, or one is a path-suffix of the other."""
    if a == b:
        return True
    return a.endswith("/" + b.lstrip("./")) or b.endswith("/" + a.lstrip("./"))


def _dedupe_keep_order(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _receipt(entry: dict[str, Any], sid: str | None, parent: _Parent | None) -> BriefReceipt:
    def s(key: str) -> str | None:
        v = entry.get(key)
        return v if isinstance(v, str) else None

    def n(key: str) -> int:
        v = entry.get(key)
        return v if isinstance(v, int) and not isinstance(v, bool) else 0

    counts = entry.get("counts")
    r = BriefReceipt(
        session_id=sid,
        tool_use_id=s("tool_use_id"),
        subagent_type=s("subagent_type"),
        decision=s("decision"),
        skip_reason=s("skip_reason"),
        ts=s("ts"),
        prompt_bytes=n("prompt_bytes"),
        packet_bytes=n("packet_bytes"),
        counts={k: v for k, v in counts.items() if isinstance(v, int) and not isinstance(v, bool)}
        if isinstance(counts, dict)
        else {},
    )
    if parent is None:
        r.unmatched_reason = "no_session_id" if sid is None else "no_parent_transcript"
        return r

    children = parent.children()
    child: _Child | None = None
    tid = r.tool_use_id
    aid = parent.agent_ids.get(tid) if tid else None
    if aid is not None:
        child = children.get(aid)
        r.agent_id = aid
        r.join = "tool_use_id"
    sha = s("packet_sha16")
    if child is None and sha:
        hits = [c for c in children.values() if c.packet_sha16 == sha]
        if len(hits) == 1:
            child = hits[0]
            r.agent_id = child.agent_id
            r.join = "packet_sha16"
    if tid and tid in parent.results:
        status, is_error = parent.results[tid]
        r.outcome = "error" if is_error else (status or "completed")
    elif tid and tid in parent.agent_calls:
        r.outcome = "no_result"
    if child is None:
        if aid is not None:
            r.unmatched_reason = "no_child_transcript"
        elif tid and tid in parent.agent_calls:
            r.unmatched_reason = "no_agent_link"
        else:
            r.unmatched_reason = "tool_use_not_in_parent"
        r.join = None
        return r

    r.matched = True
    r.target_model = child.model
    r.packet_items = child.packet_items if child.packet else None
    r.briefed_files = _dedupe_keep_order(child.briefed)
    r.child_files_read = _dedupe_keep_order(child.reads)
    r.child_tokens = child.tokens
    r.child_turns = child.turns
    r.compactions = child.compactions
    r.overflows = child.overflows

    briefed_n = [_norm(p) for p in r.briefed_files]
    child_n = [_norm(p) for p in r.child_files_read]
    r.rereads_of_briefed = [p for p in r.briefed_files if any(_same(_norm(p), c) for c in child_n)]
    call_idx = parent.agent_calls.get(tid) if tid else None
    parent_seen = _dedupe_keep_order(
        _norm(p) for i, p in parent.reads if call_idx is None or i < call_idx
    )
    r.parent_read_not_briefed = [
        p
        for p in r.child_files_read
        if any(_same(_norm(p), q) for q in parent_seen)
        and not any(_same(_norm(p), b) for b in briefed_n)
    ]
    return r
