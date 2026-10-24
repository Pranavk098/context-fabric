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

_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder[^>]*>.*?</system-reminder>", re.DOTALL)
_TYPE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

# Composition categories, in report order.
_COMP_CATEGORIES = (
    "assistant_text",
    "assistant_thinking",
    "tool_use_input",
    "tool_result",
    "user_text",
    "system_reminder",
    "hook_additional_context",
    "compact_summary",
    "image",
    "other",
)
_TOOL_INPUT_BUCKETS = ("Edit", "Write", "Bash", "Agent", "other")


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


def _attachment_category(att_type: object) -> str:
    """Name an attachment line's category from its ``attachment.type`` (open-ended)."""
    if isinstance(att_type, str) and _TYPE_NAME_RE.fullmatch(att_type):
        return "attachment:" + att_type
    return "attachment:other"


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


def _json_bytes(value: Any) -> int:
    """UTF-8 byte length of a value's JSON serialisation (counts/sizes only)."""
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _reminder_split(text: str) -> tuple[int, int, int]:
    """Return (reminder bytes, remaining bytes, span count) for one text string."""
    total = len(text.encode("utf-8"))
    rem = 0
    spans = 0
    for match in _SYSTEM_REMINDER_RE.finditer(text):
        rem += len(match.group(0).encode("utf-8"))
        spans += 1
    return rem, total - rem, spans


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

    # Composition accumulators (counts and sizes only).
    cat_bytes = {c: 0 for c in _COMP_CATEGORIES}
    cat_count = {c: 0 for c in _COMP_CATEGORIES}
    tool_input = {b: {"bytes": 0, "count": 0} for b in _TOOL_INPUT_BUCKETS}
    other_types: dict[str, int] = {}
    prefix_bytes = {c: 0 for c in _COMP_CATEGORIES}
    assistant_seen = 0
    total_bytes = 0
    first_turn_tokens = 0
    visible_before_first = 0

    def _add(cat: str, nbytes: int, count: int, prefix: bool) -> None:
        nonlocal total_bytes
        cat_bytes[cat] = cat_bytes.get(cat, 0) + nbytes
        cat_count[cat] = cat_count.get(cat, 0) + count
        total_bytes += nbytes
        if prefix:
            prefix_bytes[cat] = prefix_bytes.get(cat, 0) + nbytes

    def _other(name: object, nbytes: int, count: int, prefix: bool) -> None:
        _add("other", nbytes, count, prefix)
        key = name if isinstance(name, str) and _TYPE_NAME_RE.fullmatch(name) else "other"
        other_types[key] = other_types.get(key, 0) + count

    for obj in _iter_objects(path):
        obj_type = obj.get("type")
        is_assistant = obj_type == "assistant"
        # First 3 assistant turns: assistant content plus everything before turn 4.
        prefix = (assistant_seen < 3) if is_assistant else (assistant_seen <= 3)

        if "isCompactSummary" in obj or obj.get("subtype") == "compact_boundary":
            compactions += 1
            has_compaction = True
            after_last = 0
            _add("compact_summary", _json_bytes(obj), 1, prefix)
            continue

        if obj_type == "attachment":
            att = obj.get("attachment")
            att_type = att.get("type") if isinstance(att, dict) else None
            if isinstance(att_type, str) and att_type.startswith("hook_"):
                _add("hook_additional_context", _json_bytes(obj), 1, prefix)
            else:
                _add(_attachment_category(att_type), _json_bytes(obj), 1, prefix)
            continue
        if obj_type == "system":
            if "hookAdditionalContext" in obj or obj.get("subtype") == "stop_hook_summary":
                _add("hook_additional_context", _json_bytes(obj), 1, prefix)
            else:
                _other("system", _json_bytes(obj), 1, prefix)
            continue
        if obj_type not in ("assistant", "user"):
            _other(obj_type, _json_bytes(obj), 1, prefix)
            continue

        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
        tool_results = [b for b in blocks if b.get("type") == "tool_result"]

        usage = msg.get("usage") if isinstance(msg, dict) else None
        usage = usage if isinstance(usage, dict) else None
        if is_assistant:
            if assistant_seen == 0:
                visible_before_first = total_bytes
                if usage is not None:
                    first_turn_tokens = (
                        (_as_int(usage.get("input_tokens")) or 0)
                        + (_as_int(usage.get("cache_creation_input_tokens")) or 0)
                        + (_as_int(usage.get("cache_read_input_tokens")) or 0)
                    )
            if usage is not None:
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

        # Composition: block-level content.
        if is_assistant:
            for block in blocks:
                bt = block.get("type")
                if bt == "text":
                    t = block.get("text")
                    if isinstance(t, str):
                        _add("assistant_text", len(t.encode("utf-8")), 1, prefix)
                elif bt == "thinking":
                    t = block.get("thinking")
                    if isinstance(t, str):
                        _add("assistant_thinking", len(t.encode("utf-8")), 1, prefix)
                elif bt == "tool_use":
                    inp = block.get("input")
                    nbytes = _json_bytes(inp) if inp is not None else 0
                    _add("tool_use_input", nbytes, 1, prefix)
                    name = block.get("name")
                    bucket = (
                        name if isinstance(name, str) and name in _TOOL_INPUT_BUCKETS else "other"
                    )
                    tool_input[bucket]["bytes"] += nbytes
                    tool_input[bucket]["count"] += 1
                else:
                    _other(bt, _json_bytes(block), 1, prefix)
            assistant_seen += 1
        else:
            for block in blocks:
                bt = block.get("type")
                if bt == "text":
                    t = block.get("text")
                    if isinstance(t, str):
                        rem, rest, spans = _reminder_split(t)
                        _add("system_reminder", rem, spans, prefix)
                        _add("user_text", rest, 1, prefix)
                elif bt == "tool_result":
                    texts = _texts(block.get("content"))
                    total = sum(len(t.encode("utf-8")) for t in texts)
                    rem = spans = 0
                    for t in texts:
                        r, _, s = _reminder_split(t)
                        rem += r
                        spans += s
                    _add("tool_result", total - rem, 1, prefix)
                    _add("system_reminder", rem, spans, prefix)
                elif bt == "image":
                    src = block.get("source")
                    data = src.get("data") if isinstance(src, dict) else None
                    _add("image", len(data) if isinstance(data, str) else 0, 1, prefix)
                else:
                    _other(bt, _json_bytes(block), 1, prefix)

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
        "comp_categories": {c: {"bytes": cat_bytes[c], "count": cat_count[c]} for c in cat_bytes},
        "comp_tool_input": tool_input,
        "comp_other_types": other_types,
        "comp_prefix": prefix_bytes,
        "comp_total": total_bytes,
        "first_turn_tokens": first_turn_tokens,
        "visible_before_first": visible_before_first,
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


def _group_summary(
    bytes_: dict[str, int],
    counts: dict[str, int],
    tool_input: dict[str, dict[str, int]],
    other_types: dict[str, int],
) -> dict[str, Any]:
    """Build one group's composition block from aggregated category bytes/counts."""
    total = sum(bytes_.values())
    categories: dict[str, Any] = {}
    for cat, nbytes in bytes_.items():
        entry: dict[str, Any] = {"bytes": nbytes, "count": counts.get(cat, 0)}
        if cat == "tool_use_input":
            entry["by_tool"] = tool_input
        elif cat == "other":
            entry["types"] = other_types
        categories[cat] = entry
    shares = {cat: round(nbytes / total, 4) if total else 0.0 for cat, nbytes in bytes_.items()}
    return {
        "total_bytes": total,
        "categories": categories,
        "shares": shares,
        "estimated_tokens": {cat: round(nbytes / 4.0, 1) for cat, nbytes in bytes_.items()},
    }


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

    comp_bytes = {c: 0 for c in _COMP_CATEGORIES}
    comp_count = {c: 0 for c in _COMP_CATEGORIES}
    comp_tool_input = {b: {"bytes": 0, "count": 0} for b in _TOOL_INPUT_BUCKETS}
    comp_other_types: dict[str, int] = {}
    prefix_lists: dict[str, list[int]] = {c: [] for c in _COMP_CATEGORIES}
    whole_lists: dict[str, list[int]] = {c: [] for c in _COMP_CATEGORIES}
    prefix_present: dict[str, list[int]] = {}
    first_turn_tokens_list: list[int] = []
    visible_before_list: list[int] = []

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
        for cat, entry in r["comp_categories"].items():
            comp_bytes[cat] = comp_bytes.get(cat, 0) + entry["bytes"]
            comp_count[cat] = comp_count.get(cat, 0) + entry["count"]
        for tool, slot in r["comp_tool_input"].items():
            comp_tool_input[tool]["bytes"] += slot["bytes"]
            comp_tool_input[tool]["count"] += slot["count"]
        for name, cnt in r["comp_other_types"].items():
            comp_other_types[name] = comp_other_types.get(name, 0) + cnt
        if r["turns"] > 0:
            for cat in _COMP_CATEGORIES:
                prefix_lists[cat].append(r["comp_prefix"][cat])
                whole_lists[cat].append(r["comp_categories"][cat]["bytes"])
            for cat, nbytes in r["comp_prefix"].items():
                if nbytes > 0:
                    prefix_present.setdefault(cat, []).append(nbytes)
            first_turn_tokens_list.append(r["first_turn_tokens"])
            visible_before_list.append(r["visible_before_first"])

    sub_weighted = 0.0
    sub_raw_in = sub_raw_out = sub_raw_cc = sub_raw_cr = 0
    fractions: list[float] = []
    never_touched = 0

    sub_comp_bytes = {c: 0 for c in _COMP_CATEGORIES}
    sub_comp_count = {c: 0 for c in _COMP_CATEGORIES}
    sub_comp_tool_input = {b: {"bytes": 0, "count": 0} for b in _TOOL_INPUT_BUCKETS}
    sub_comp_other_types: dict[str, int] = {}

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
        for cat, entry in r["comp_categories"].items():
            sub_comp_bytes[cat] = sub_comp_bytes.get(cat, 0) + entry["bytes"]
            sub_comp_count[cat] = sub_comp_count.get(cat, 0) + entry["count"]
        for tool, slot in r["comp_tool_input"].items():
            sub_comp_tool_input[tool]["bytes"] += slot["bytes"]
            sub_comp_tool_input[tool]["count"] += slot["count"]
        for name, cnt in r["comp_other_types"].items():
            sub_comp_other_types[name] = sub_comp_other_types.get(name, 0) + cnt

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

    parents_group = _group_summary(comp_bytes, comp_count, comp_tool_input, comp_other_types)
    parents_group["starting_prefix_median_bytes"] = {
        cat: round(_pctl(prefix_lists[cat], 50), 1) for cat in _COMP_CATEGORIES
    }
    parents_group["whole_session_median_bytes"] = {
        cat: round(_pctl(whole_lists[cat], 50), 1) for cat in _COMP_CATEGORIES
    }
    ratios = [
        tok / (vb / 4.0)
        for tok, vb in zip(first_turn_tokens_list, visible_before_list, strict=True)
        if vb > 0
    ]
    visible_bytes_median = _pctl(visible_before_list, 50)
    parents_group["first_turn"] = {
        "usage_tokens_median": round(_pctl(first_turn_tokens_list, 50), 1),
        "visible_bytes_median": round(visible_bytes_median, 1),
        "visible_tokens_estimated_median": round(visible_bytes_median / 4.0, 1),
        "ratio_median": round(percentile(ratios, 50), 3),
    }
    by_item: dict[str, dict[str, Any]] = {}
    for cat, vals in prefix_present.items():
        med = _pctl(vals, 50)
        by_item[cat] = {
            "median_bytes": round(med, 1),
            "estimated_tokens": round(med / 4.0, 1),
            "sessions_present": len(vals),
        }
    parents_group["starting_prefix_by_item"] = by_item
    parents_group["starting_prefix_by_item_total_median_bytes"] = round(
        sum(v["median_bytes"] for v in by_item.values()), 1
    )
    subagents_group = _group_summary(
        sub_comp_bytes, sub_comp_count, sub_comp_tool_input, sub_comp_other_types
    )

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
        "composition": {"parents": parents_group, "subagents": subagents_group},
    }


def _comp_group_lines(label: str, group: dict[str, Any]) -> list[str]:
    """Human lines for one composition group: top 8 categories by bytes."""
    total = group["total_bytes"]
    lines = [
        f"Context composition ({label}):",
        f"  total {total:,} bytes (~{total / 4:,.0f} est. tokens)",
    ]
    ranked = sorted(group["categories"].items(), key=lambda kv: (-kv[1]["bytes"], kv[0]))
    for cat, st in ranked[:8]:
        share = group["shares"][cat]
        line = (
            f"  {cat}: {st['count']:,} blocks, {st['bytes']:,} bytes "
            f"({share * 100:.1f}%, ~{st['bytes'] / 4:,.0f} est. tokens)"
        )
        if cat == "tool_use_input":
            bt = st["by_tool"]
            line += " (" + ", ".join(f"{t} {bt[t]['bytes']:,}" for t in _TOOL_INPUT_BUCKETS) + ")"
        if cat == "other":
            types = st.get("types", {})
            if types:
                top = ", ".join(
                    f"{k} {v:,}" for k, v in sorted(types.items(), key=lambda kv: -kv[1])[:8]
                )
                line += f" [types: {top}]"
        lines.append(line)
    rest = ranked[8:]
    if rest:
        rest_share = sum(st["bytes"] for _, st in rest) / total if total else 0.0
        lines.append(f"  other categories: {len(rest)}, {rest_share * 100:.1f}%")
    return lines


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
    lines.extend(_comp_group_lines("parents", report["composition"]["parents"]))
    pg = report["composition"]["parents"]
    lines.append("Starting prefix (first 3 turns, parents) median bytes / whole-session median:")
    for cat in _COMP_CATEGORIES:
        lines.append(
            f"  {cat}: {pg['starting_prefix_median_bytes'][cat]:,.0f} / "
            f"{pg['whole_session_median_bytes'][cat]:,.0f}"
        )
    ft = pg["first_turn"]
    lines.append(
        f"First-turn context (parents): median usage {ft['usage_tokens_median']:,.0f} tokens vs "
        f"{ft['visible_tokens_estimated_median']:,.0f} est. visible tokens -> "
        f"ratio {ft['ratio_median']:.2f}"
    )
    by_item = pg["starting_prefix_by_item"]
    lines.append("Starting prefix by item (first 3 assistant turns, parents):")
    for cat, item in sorted(by_item.items(), key=lambda kv: -kv[1]["median_bytes"]):
        lines.append(
            f"  {cat}: {item['median_bytes']:,.0f} bytes (~{item['estimated_tokens']:,.0f} "
            f"est. tokens) in {item['sessions_present']:,} sessions"
        )
    total_by_item = pg["starting_prefix_by_item_total_median_bytes"]
    lines.append(
        f"  typical starting prefix visible in the transcript: {total_by_item:,.0f} bytes "
        f"(~{total_by_item / 4:,.0f} est. tokens)"
    )
    lines.extend(_comp_group_lines("sub-agents", report["composition"]["subagents"]))
    lines.append("This report contains counts and sizes only; no paths, commands or text.")
    return "\n".join(lines)
