"""``cfxd audit``: offline, counts-only analysis of Claude Code transcripts.

Reads only counts, sizes, tool names and in-memory hashes of paths — never
paths, commands, prompts, tool output or any transcript text. The report
contains aggregate numbers only, so testers can run it on private transcripts
and paste the result back.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cfxd.daemon import percentile

DEFAULT_ROOT = Path.home() / ".claude" / "projects"

_SINCE_DELTAS = {"7d": timedelta(days=7), "30d": timedelta(days=30)}
_SINCE_LABELS = {"7d": "last 7 days", "30d": "last 30 days", "all": "all time"}

_MAX_LINE_BYTES = 20 * 1024 * 1024
_TEN_KB = 10 * 1024
_TWENTYFIVE_KB = 25 * 1024

# Saved-to-disk notice phrases (metric 4), matched case-sensitively as given.
_PERSISTED_PHRASES = ("Output too large", "persisted-output", "Full output saved to")
# Native "file unchanged" dedup notice (metric 5), matched case-insensitively.
_UNCHANGED_PHRASES = ("file unchanged", "<file unchanged>")

_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_MCP_SERVER_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _tool_bucket(name: str) -> str:
    """Collapse ``mcp__<server>__<tool>`` to ``mcp__<server>`` (or ``mcp__other``)."""
    if name.startswith("mcp__"):
        server = name[len("mcp__") :].split("__", 1)[0]
        if _MCP_SERVER_RE.fullmatch(server):
            return "mcp__" + server
        return "mcp__other"
    return name


def _texts(content: Any) -> list[str]:
    """Flatten a tool-result ``content`` (str or list of blocks) to its text strings."""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        out: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    out.append(text)
        return out
    return []


def _weighted(usage: dict[str, Any]) -> float:
    i = _as_int(usage.get("input_tokens")) or 0
    cc = _as_int(usage.get("cache_creation_input_tokens")) or 0
    cr = _as_int(usage.get("cache_read_input_tokens")) or 0
    o = _as_int(usage.get("output_tokens")) or 0
    return i + 1.25 * cc + 0.1 * cr + 5.0 * o


def _file_path(inp: dict[str, Any]) -> str | None:
    fp = inp.get("file_path")
    return fp if isinstance(fp, str) else None


def _touch_paths(block: dict[str, Any]) -> list[str]:
    """Path strings a Read/Edit/Grep/Glob tool_use touches (for sub-agent metric 7)."""
    name = block.get("name")
    inp = block.get("input")
    if not isinstance(inp, dict):
        return []
    if name in ("Read", "Edit"):
        fp = inp.get("file_path")
        return [fp] if isinstance(fp, str) else []
    if name == "Grep":
        p = inp.get("path")
        return [p] if isinstance(p, str) else []
    if name == "Glob":
        out: list[str] = []
        pattern = inp.get("pattern")
        p = inp.get("path")
        if isinstance(pattern, str):
            out.append(pattern)
        if isinstance(p, str):
            out.append(p)
        return out
    return []


def _touches(block: dict[str, Any], known: set[int]) -> bool:
    return any(hash(p) in known for p in _touch_paths(block))


def _iter_objects(path: Path) -> Iterator[dict[str, Any]]:
    """Stream JSON objects from a transcript, skipping malformed/oversized lines."""
    try:
        fh = open(path, "rb")
    except OSError:
        return
    with fh:
        for raw in fh:
            if len(raw) > _MAX_LINE_BYTES:
                continue
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


def _scan(path: Path, known: set[int] | None = None) -> dict[str, Any]:
    """Stream one transcript; return per-session aggregates.

    ``known`` is the parent session's Read/Edit path hashes (passed only for
    sub-agent sessions, to compute metric 7). For parent sessions it is None
    and the session's own Read/Edit path hashes are collected and returned.
    """
    id_name: dict[str, str] = {}
    read_seen: set[int] = set()
    repeat_ids: set[str] = set()
    repeat_read_count = 0
    read_result_bytes = 0
    repeat_read_bytes = 0
    unchanged_count = 0

    turns = 0
    after_last = 0
    compactions = 0
    has_compaction = False

    tool_sizes: dict[str, list[int]] = {}
    persisted_count = 0
    persisted_phrases: dict[str, int] = {p: 0 for p in _PERSISTED_PHRASES}

    weighted = 0.0
    raw_in = raw_out = raw_cc = raw_cr = 0
    before_first_touch: float | None = None
    touched = False

    own_known: set[int] = set()

    for obj in _iter_objects(path):
        if "isCompactSummary" in obj or obj.get("subtype") == "compact_boundary":
            compactions += 1
            has_compaction = True
            after_last = 0

        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
        tool_results = [b for b in blocks if b.get("type") == "tool_result"]

        usage = msg.get("usage") if isinstance(msg, dict) else None
        usage = usage if isinstance(usage, dict) else None
        if obj.get("type") == "assistant" and usage is not None:
            if known is not None and not touched and any(_touches(b, known) for b in tool_uses):
                before_first_touch = weighted
                touched = True
            turns += 1
            after_last += 1
            weighted += _weighted(usage)
            raw_in += _as_int(usage.get("input_tokens")) or 0
            raw_out += _as_int(usage.get("output_tokens")) or 0
            raw_cc += _as_int(usage.get("cache_creation_input_tokens")) or 0
            raw_cr += _as_int(usage.get("cache_read_input_tokens")) or 0

        for block in tool_uses:
            name = block.get("name")
            bid = block.get("id")
            inp = block.get("input")
            if not isinstance(name, str) or not isinstance(bid, str):
                continue
            id_name[bid] = name
            if not isinstance(inp, dict):
                continue
            fp = _file_path(inp)
            if name == "Read" and fp is not None:
                path_hash = hash(fp)
                if known is None:
                    own_known.add(path_hash)
                if "offset" not in inp and "limit" not in inp:
                    if path_hash in read_seen:
                        repeat_read_count += 1
                        repeat_ids.add(bid)
                    else:
                        read_seen.add(path_hash)
            elif name in _EDIT_TOOLS and fp is not None:
                path_hash = hash(fp)
                read_seen.discard(path_hash)
                if known is None:
                    own_known.add(path_hash)

        for block in tool_results:
            tid = block.get("tool_use_id")
            name = id_name.get(tid) if isinstance(tid, str) else None
            if name is None:
                continue
            texts = _texts(block.get("content"))
            nbytes = sum(len(t.encode("utf-8")) for t in texts)
            bucket = _tool_bucket(name)
            tool_sizes.setdefault(bucket, []).append(nbytes)
            if any(phrase in t for t in texts for phrase in _PERSISTED_PHRASES):
                persisted_count += 1
            for phrase in _PERSISTED_PHRASES:
                if any(phrase in t for t in texts):
                    persisted_phrases[phrase] += 1
            if any(phrase in t.lower() for t in texts for phrase in _UNCHANGED_PHRASES):
                unchanged_count += 1
            if name == "Read":
                read_result_bytes += nbytes
                if tid in repeat_ids:
                    repeat_read_bytes += nbytes

    return {
        "turns": turns,
        "after_last": after_last if has_compaction else None,
        "compactions": compactions,
        "tool_sizes": tool_sizes,
        "persisted_count": persisted_count,
        "persisted_phrases": persisted_phrases,
        "unchanged_count": unchanged_count,
        "repeat_read_count": repeat_read_count,
        "read_result_bytes": read_result_bytes,
        "repeat_read_bytes": repeat_read_bytes,
        "weighted": weighted,
        "raw_in": raw_in,
        "raw_out": raw_out,
        "raw_cc": raw_cc,
        "raw_cr": raw_cr,
        "before_first_touch": before_first_touch,
        "touched": touched,
        "own_known": own_known,
    }


def _in_window(path: Path, cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return datetime.fromtimestamp(mtime, UTC) >= cutoff


def _discover(root: Path, cutoff: datetime | None) -> tuple[list[Path], list[tuple[Path, str]]]:
    parents: list[Path] = []
    subs: list[tuple[Path, str]] = []
    if not root.is_dir():
        return parents, subs
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if not fn.endswith(".jsonl"):
                continue
            path = Path(dirpath) / fn
            if not _in_window(path, cutoff):
                continue
            parts = path.parts
            if "subagents" in parts:
                idx = parts.index("subagents")
                subs.append((path, parts[idx - 1] if idx >= 1 else ""))
            else:
                parents.append(path)
    return parents, subs


def _pctl(values: list[int], p: float) -> float:
    return percentile([float(v) for v in values], p)


def audit(root: Path, since: str = "30d", now: datetime | None = None) -> dict[str, Any]:
    """Aggregate counts-only audit stats over every transcript under ``root``."""
    if now is None:
        now = datetime.now(UTC)
    delta = _SINCE_DELTAS.get(since)
    cutoff = now - delta if delta is not None else None
    parents, subs = _discover(root, cutoff)

    turn_counts: list[int] = []
    tool_sizes: dict[str, list[int]] = {}
    persisted_count = 0
    persisted_phrases: dict[str, int] = {p: 0 for p in _PERSISTED_PHRASES}
    unchanged_count = 0
    repeat_read_count = 0
    read_result_bytes = 0
    repeat_read_bytes = 0
    compactions = 0
    after_last_counts: list[int] = []

    parent_known: dict[str, set[int]] = {}

    for path in parents:
        r = _scan(path, known=None)
        turn_counts.append(r["turns"])
        for bucket, sizes in r["tool_sizes"].items():
            tool_sizes.setdefault(bucket, []).extend(sizes)
        persisted_count += r["persisted_count"]
        for ph, c in r["persisted_phrases"].items():
            persisted_phrases[ph] += c
        unchanged_count += r["unchanged_count"]
        repeat_read_count += r["repeat_read_count"]
        read_result_bytes += r["read_result_bytes"]
        repeat_read_bytes += r["repeat_read_bytes"]
        compactions += r["compactions"]
        if r["after_last"] is not None:
            after_last_counts.append(r["after_last"])
        parent_known[path.stem] = r["own_known"]

    sub_weighted = 0.0
    sub_raw_in = sub_raw_out = sub_raw_cc = sub_raw_cr = 0
    fractions: list[float] = []
    never_touched = 0

    for path, parent_id in subs:
        r = _scan(path, known=parent_known.get(parent_id, set()))
        sub_weighted += r["weighted"]
        sub_raw_in += r["raw_in"]
        sub_raw_out += r["raw_out"]
        sub_raw_cc += r["raw_cc"]
        sub_raw_cr += r["raw_cr"]
        if r["touched"] and r["weighted"] > 0:
            fractions.append((r["before_first_touch"] or 0.0) / r["weighted"])
        else:
            never_touched += 1

    # Metric 2/3: per-tool byte stats and share of bytes from >10 KB results.
    total_bytes = sum(sum(sizes) for sizes in tool_sizes.values())
    tools: dict[str, dict[str, Any]] = {}
    share_over_10k: dict[str, float] = {}
    for bucket in sorted(tool_sizes, key=lambda b: -sum(tool_sizes[b])):
        sizes = tool_sizes[bucket]
        over_10 = [s for s in sizes if s > _TEN_KB]
        over_25 = [s for s in sizes if s > _TWENTYFIVE_KB]
        tools[bucket] = {
            "count": len(sizes),
            "bytes": sum(sizes),
            "p50": round(_pctl(sizes, 50), 1),
            "p90": round(_pctl(sizes, 90), 1),
            "max": max(sizes) if sizes else 0,
            "over_10k": len(over_10),
            "over_25k": len(over_25),
        }
        if total_bytes:
            share_over_10k[bucket] = round(sum(over_10) / total_bytes, 4)

    return {
        "since": since,
        "sessions": len(parents),
        "subagent_sessions": len(subs),
        "turns": {
            "total": sum(turn_counts),
            "median": round(_pctl(turn_counts, 50), 1),
            "p90": round(_pctl(turn_counts, 90), 1),
            "sessions_over_30": sum(1 for t in turn_counts if t > 30),
        },
        "tools": tools,
        "share_over_10k": share_over_10k,
        "persisted_output": {"count": persisted_count, "phrases": persisted_phrases},
        "repeated_reads": {
            "count": repeat_read_count,
            "repeat_bytes_share": round(repeat_read_bytes / read_result_bytes, 4)
            if read_result_bytes
            else 0.0,
            "file_unchanged_dedup": unchanged_count,
        },
        "compactions": {
            "count": compactions,
            "turns_after_last_median": round(_pctl(after_last_counts, 50), 1),
            "turns_after_last_p90": round(_pctl(after_last_counts, 90), 1),
            "sessions_with_compaction": len(after_last_counts),
        },
        "subagents": {
            "count": len(subs),
            "weighted_tokens_total": round(sub_weighted, 1),
            "raw": {
                "input_tokens": sub_raw_in,
                "output_tokens": sub_raw_out,
                "cache_creation_input_tokens": sub_raw_cc,
                "cache_read_input_tokens": sub_raw_cr,
            },
            "first_touch_fraction": {
                "median": round(percentile(fractions, 50), 4) if fractions else 0.0,
                "mean": round(sum(fractions) / len(fractions), 4) if fractions else 0.0,
            },
            "never_touched": never_touched,
        },
    }


def format_human(report: dict[str, Any], dir_name: str = ".") -> str:
    """Render the audit report as short plain text (counts and sizes only)."""
    if report["sessions"] == 0 and report["subagent_sessions"] == 0:
        return f"No transcripts found under {dir_name}."
    label = _SINCE_LABELS.get(str(report["since"]), str(report["since"]))
    lines = [
        f"Claude Code transcripts, {label}",
        f"Sessions: {report['sessions']:,}   sub-agent sessions: {report['subagent_sessions']:,}",
    ]
    t = report["turns"]
    lines.append(
        f"Turns: {t['total']:,} total, median {t['median']:.1f}, p90 {t['p90']:.1f}, "
        f"{t['sessions_over_30']} sessions over 30 turns"
    )
    lines.append("Tool-result bytes:")
    for bucket, st in report["tools"].items():
        lines.append(
            f"  {bucket}: {st['count']:,} results, {st['bytes']:,} bytes "
            f"(p50 {st['p50']:.0f}, p90 {st['p90']:.0f}, max {st['max']:,}, "
            f"{st['over_10k']} over 10 KB, {st['over_25k']} over 25 KB)"
        )
    shares = ", ".join(f"{k} {v * 100:.1f}%" for k, v in report["share_over_10k"].items())
    lines.append(f"Share of tool-result bytes over 10 KB: {shares}")
    po = report["persisted_output"]
    lines.append(f"Persisted-output stubs: {po['count']}")
    rr = report["repeated_reads"]
    lines.append(
        f"Repeated full Reads: {rr['count']} "
        f"({rr['repeat_bytes_share'] * 100:.1f}% of Read result bytes), "
        f"file-unchanged dedup: {rr['file_unchanged_dedup']}"
    )
    c = report["compactions"]
    lines.append(
        f"Compactions: {c['count']} boundaries, turns after last compaction "
        f"median {c['turns_after_last_median']:.1f}, p90 {c['turns_after_last_p90']:.1f}"
    )
    sa = report["subagents"]
    lines.append(
        f"Sub-agents: {sa['count']:,}, weighted tokens {sa['weighted_tokens_total']:,.1f}, "
        f"first-touch fraction median {sa['first_touch_fraction']['median']:.3f}, "
        f"mean {sa['first_touch_fraction']['mean']:.3f}, "
        f"{sa['never_touched']} never touched a known file"
    )
    lines.append("This report contains counts and sizes only; no paths, commands or text.")
    return "\n".join(lines)
