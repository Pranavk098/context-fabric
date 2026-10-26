"""``cfxd refetch``: how much of the context dropped by a compaction is re-read.

Counts and numbers only — never paths, commands, prompts, tool output or input
hashes. Held items are SHA-256 hashes kept in memory for one run and discarded;
the report contains aggregate numbers only (same rule and canary test as
``cfxd audit``).

For every recorded compaction boundary the stream gives the context before it
(last assistant turn's ``input + cache_creation + cache_read``) and after it
(first assistant turn after), so the dropped context is ``D = C_before -
C_after``. Held items are file paths read/edited before the boundary and
tool-call signatures (name + SHA-256 of canonical input, ``cfxd.waste``) of
Bash/Grep/Glob/WebFetch calls whose result was over 2 KB. In the next ``window``
assistant turns, every tool result that matches a held item is a re-read:
``f = min(1, re_read_tokens / D)`` with tool-result tokens ``bytes / 4``. A
re-read of the same item counts again, because it costs again.

``/clear`` restarts are linked only when unambiguous: a parent session in the
same project directory whose last assistant turn precedes the new session's
first assistant turn, with at most one candidate within 10 minutes and a first
user message that is not a continuation marker. Otherwise they are skipped.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cfxd.audit import (
    _SINCE_DELTAS,
    _SINCE_LABELS,
    _as_int,
    _discover,
    _iter_objects,
    _texts,
)
from cfxd.daemon import percentile
from cfxd.replay import _parse_ts, replay
from cfxd.waste import _signature

_LARGE_RESULT_BYTES = 2048
_FILE_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"})
_SIG_TOOLS = frozenset({"Bash", "Grep", "Glob", "WebFetch"})
_CLEAR_GAP = timedelta(minutes=10)
_CONTINUATION_MARKERS = (
    "This session is being continued from a previous conversation",
    "continued from a previous conversation",
)
_TRIGGERS = ("manual", "auto", "unknown", "clear")
_KILL_THRESHOLD_PCT = 20.0
_RESET_K = 40
_SUMMARY_TOKENS = (2000, 5000)

_ItemKey = tuple[str, str]


@dataclass
class _Turn:
    ctx: int
    ts: datetime | None


@dataclass
class _Boundary:
    before: int
    after: int
    held: frozenset[_ItemKey]
    trigger: str


@dataclass
class _Scan:
    turns: list[_Turn] = field(default_factory=list)
    results: list[tuple[int, _ItemKey, int]] = field(default_factory=list)
    boundaries: list[_Boundary] = field(default_factory=list)
    held: set[_ItemKey] = field(default_factory=set)
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    continuation_start: bool = False


def _ctx(usage: dict[str, Any] | None) -> int | None:
    """Context size of one turn: input + cache creation + cache read."""
    if usage is None:
        return None
    parts = (
        _as_int(usage.get("input_tokens")),
        _as_int(usage.get("cache_creation_input_tokens")),
        _as_int(usage.get("cache_read_input_tokens")),
    )
    if all(p is None for p in parts):
        return None
    return sum(p or 0 for p in parts)


def _path_key(file_path: str) -> _ItemKey:
    return ("path", hashlib.sha256(file_path.encode("utf-8")).hexdigest())


def _trigger(obj: dict[str, Any]) -> str:
    meta = obj.get("compactMetadata")
    if isinstance(meta, dict):
        value = meta.get("trigger")
        if value in ("manual", "auto"):
            return str(value)
    value = obj.get("trigger")
    if value in ("manual", "auto"):
        return str(value)
    return "unknown"


def _scan(path: Path) -> _Scan:
    """Stream one transcript into turns, held-item results and boundaries."""
    scan = _Scan()
    id_item: dict[str, _ItemKey] = {}
    pending_large: dict[str, _ItemKey] = {}
    pending_results: list[tuple[_ItemKey, int]] = []
    first_user_seen = False

    for obj in _iter_objects(path):
        if "isCompactSummary" in obj or obj.get("subtype") == "compact_boundary":
            if "isCompactSummary" in obj and not scan.turns:
                scan.continuation_start = True
            scan.boundaries.append(
                _Boundary(
                    before=len(scan.turns) - 1,
                    after=len(scan.turns),
                    held=frozenset(scan.held),
                    trigger=_trigger(obj),
                )
            )
            continue

        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []

        if obj.get("type") == "user" and not first_user_seen:
            first_user_seen = True
            if any(m in t for t in _texts(content) for m in _CONTINUATION_MARKERS):
                scan.continuation_start = True

        ts = _parse_ts(obj.get("timestamp"))
        if ts is None and isinstance(msg, dict):
            ts = _parse_ts(msg.get("timestamp"))

        usage = msg.get("usage") if isinstance(msg, dict) else None
        usage = usage if isinstance(usage, dict) else None
        ctx = _ctx(usage)

        if obj.get("type") == "assistant" and ctx is not None:
            # Results seen since the previous usage turn enter context here.
            for key, nbytes in pending_results:
                scan.results.append((len(scan.turns), key, nbytes))
            pending_results = []
            for block in blocks:
                if block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                bid = block.get("id")
                inp = block.get("input")
                if not isinstance(name, str) or not isinstance(bid, str):
                    continue
                if not isinstance(inp, dict):
                    continue
                if name in _FILE_TOOLS:
                    file_path = inp.get("file_path")
                    if isinstance(file_path, str):
                        key = _path_key(file_path)
                        scan.held.add(key)
                        id_item[bid] = key
                elif name in _SIG_TOOLS:
                    key = ("sig", _signature(name, inp))
                    id_item[bid] = key
                    pending_large[bid] = key
            scan.turns.append(_Turn(ctx=ctx, ts=ts))
            if ts is not None:
                if scan.first_ts is None:
                    scan.first_ts = ts
                scan.last_ts = ts

        for block in blocks:
            if block.get("type") != "tool_result":
                continue
            tid = block.get("tool_use_id")
            if not isinstance(tid, str):
                continue
            result_key = id_item.get(tid)
            if result_key is None:
                continue
            nbytes = sum(len(t.encode("utf-8")) for t in _texts(block.get("content")))
            large = pending_large.pop(tid, None)
            if large is not None and nbytes > _LARGE_RESULT_BYTES:
                scan.held.add(large)
            pending_results.append((result_key, nbytes))

    return scan


def _event(
    scan: _Scan,
    before: int,
    after: int,
    held: frozenset[_ItemKey] | set[_ItemKey],
    window: int,
    source: str,
    trigger: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """One event from a boundary, or a skip reason."""
    if before < 0 or after >= len(scan.turns):
        return None, "missing_usage"
    c_before = scan.turns[before].ctx
    c_after = scan.turns[after].ctx
    dropped = c_before - c_after
    if dropped <= 0:
        return None, "nonpositive_drop"
    end = after + window
    refetched_bytes = sum(
        nbytes for entered, key, nbytes in scan.results if after <= entered < end and key in held
    )
    turns_after = len(scan.turns) - after
    return (
        {
            "source": source,
            "trigger": trigger,
            "c_before": c_before,
            "c_after": c_after,
            "d_tokens": dropped,
            "f": round(min(1.0, (refetched_bytes / 4.0) / dropped), 4),
            "turns_in_window": min(window, turns_after),
            "truncated": turns_after < window,
        },
        None,
    )


def _restart_event(
    pred: _Scan, scan: _Scan, window: int
) -> tuple[dict[str, Any] | None, str | None]:
    """One ``/clear`` event: the old session's tail drops, the new one re-reads."""
    if not pred.turns or not scan.turns:
        return None, "missing_usage"
    c_before = pred.turns[-1].ctx
    c_after = scan.turns[0].ctx
    dropped = c_before - c_after
    if dropped <= 0:
        return None, "nonpositive_drop"
    refetched_bytes = sum(
        nbytes for entered, key, nbytes in scan.results if entered < window and key in pred.held
    )
    return (
        {
            "source": "clear_restart",
            "trigger": "clear",
            "c_before": c_before,
            "c_after": c_after,
            "d_tokens": dropped,
            "f": round(min(1.0, (refetched_bytes / 4.0) / dropped), 4),
            "turns_in_window": min(window, len(scan.turns)),
            "truncated": len(scan.turns) < window,
        },
        None,
    )


def _link_clear(scans: dict[Path, _Scan], sources: dict[Path, str], window: int) -> dict[str, Any]:
    """Link unambiguous ``/clear`` restarts; never guess when two fit."""
    by_dir: dict[Path, list[Path]] = {}
    for path, scan in scans.items():
        if sources[path] != "parent" or scan.first_ts is None or scan.last_ts is None:
            continue
        by_dir.setdefault(path.parent, []).append(path)

    events: list[dict[str, Any]] = []
    skipped = {"missing_usage": 0, "nonpositive_drop": 0}
    linked = 0
    ambiguous = 0
    for paths in by_dir.values():
        for path in paths:
            scan = scans[path]
            first_ts = scan.first_ts
            if first_ts is None or scan.continuation_start or not scan.turns:
                continue
            candidates: list[Path] = []
            for q in paths:
                if q == path:
                    continue
                q_last = scans[q].last_ts
                if q_last is not None and q_last <= first_ts and first_ts - q_last <= _CLEAR_GAP:
                    candidates.append(q)
            if len(candidates) != 1:
                if candidates:
                    ambiguous += 1
                continue
            pred = scans[candidates[0]]
            event, reason = _restart_event(pred, scan, window)
            if event is None:
                skipped[reason if reason is not None else "missing_usage"] += 1
            else:
                linked += 1
                events.append(event)

    if linked:
        note = f"{linked} linked"
    elif ambiguous:
        note = f"{ambiguous} ambiguous, skipped"
    else:
        note = "none linkable, skipped"
    return {
        "events": events,
        "skipped": skipped,
        "linked": linked,
        "ambiguous": ambiguous,
        "note": note,
    }


def _pctl(values: list[float], p: float) -> float:
    return percentile(values, p)


def _aggregate(events: list[dict[str, Any]]) -> dict[str, Any]:
    ds = [float(e["d_tokens"]) for e in events]
    fs = [float(e["f"]) for e in events]
    return {
        "events": len(events),
        "d_tokens": {
            "median": round(_pctl(ds, 50), 1),
            "p25": round(_pctl(ds, 25), 1),
            "p75": round(_pctl(ds, 75), 1),
        },
        "f": {
            "median": round(_pctl(fs, 50), 4),
            "p25": round(_pctl(fs, 25), 4),
            "p75": round(_pctl(fs, 75), 4),
            "mean": round(sum(fs) / len(fs), 4) if fs else 0.0,
        },
        "share_f_above_0_5": round(sum(1 for f in fs if f > 0.5) / len(fs), 4) if fs else 0.0,
        "turns_in_window_median": round(
            _pctl([float(e["turns_in_window"]) for e in events], 50), 1
        ),
        "window_truncated": sum(1 for e in events if e["truncated"]),
    }


def _group(events: list[dict[str, Any]], field_name: str, keys: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in keys:
        group = [e for e in events if e[field_name] == key]
        if not group:
            continue
        fs = [float(e["f"]) for e in group]
        out[key] = {
            "events": len(group),
            "d_median": round(_pctl([float(e["d_tokens"]) for e in group], 50), 1),
            "f_median": round(_pctl(fs, 50), 4),
            "f_mean": round(sum(fs) / len(fs), 4),
            "share_f_above_0_5": round(sum(1 for f in fs if f > 0.5) / len(fs), 4),
        }
    return out


def _saving(root: Path, since: str, f_median: float, f_p75: float) -> dict[str, Any]:
    """Run the replay reset model at the measured f values (same function)."""
    f_values = tuple(dict.fromkeys((f_median, f_p75)))
    model = replay(
        root,
        since,
        ks=(),
        batches=(),
        reset_ks=(_RESET_K,),
        summary_tokens=_SUMMARY_TOKENS,
        refetch=f_values,
    )
    rows: list[dict[str, Any]] = []
    for policy in model["reset_policies"]:
        for variant in policy["variants"]:
            rows.append(
                {
                    "k": policy["k"],
                    "summary_tokens": policy["summary_tokens"],
                    "variant": variant["variant"],
                    "f": variant["f"],
                    "pct_saved": variant["pct_saved"],
                    "pct_saved_affected": variant["pct_saved_affected"],
                }
            )
    best = max(
        (r["pct_saved"] for r in rows if r["f"] == f_median),
        default=0.0,
    )
    return {
        "f_median": f_median,
        "f_p75": f_p75,
        "k": _RESET_K,
        "summary_tokens": list(_SUMMARY_TOKENS),
        "rows": rows,
        "kill_threshold": {
            "pct": _KILL_THRESHOLD_PCT,
            "best_pct_saved_at_median_f": best,
            "met": best < _KILL_THRESHOLD_PCT,
        },
    }


def refetch(
    root: Path, since: str = "30d", window: int = 20, now: datetime | None = None
) -> dict[str, Any]:
    """Aggregate re-fetch of compacted context over every transcript under ``root``."""
    if now is None:
        now = datetime.now(UTC)
    delta = _SINCE_DELTAS.get(since)
    cutoff = now - delta if delta is not None else None
    parents, subs = _discover(root, cutoff)

    scans: dict[Path, _Scan] = {}
    sources: dict[Path, str] = {}
    for path in parents:
        scans[path] = _scan(path)
        sources[path] = "parent"
    for path, _parent_id in subs:
        scans[path] = _scan(path)
        sources[path] = "subagent"

    events: list[dict[str, Any]] = []
    skipped = {"missing_usage": 0, "nonpositive_drop": 0}
    for path, scan in scans.items():
        for boundary in scan.boundaries:
            event, reason = _event(
                scan,
                boundary.before,
                boundary.after,
                boundary.held,
                window,
                sources[path],
                boundary.trigger,
            )
            if event is None:
                skipped[reason if reason is not None else "missing_usage"] += 1
            else:
                events.append(event)

    clear = _link_clear(scans, sources, window)
    events.extend(clear["events"])
    for key, value in clear["skipped"].items():
        skipped[key] += value

    aggregate = _aggregate(events)
    report: dict[str, Any] = {
        "since": since,
        "window": window,
        "sessions": {"parents": len(parents), "subagents": len(subs)},
        "events_analyzed": aggregate["events"],
        "events_skipped": skipped,
        "d_tokens": aggregate["d_tokens"],
        "f": aggregate["f"],
        "share_f_above_0_5": aggregate["share_f_above_0_5"],
        "turns_in_window_median": aggregate["turns_in_window_median"],
        "window_truncated": aggregate["window_truncated"],
        "by_trigger": _group(events, "trigger", _TRIGGERS),
        "by_source": _group(events, "source", ("parent", "subagent", "clear_restart")),
        "clear_restarts": {
            "linked": clear["linked"],
            "ambiguous": clear["ambiguous"],
            "note": clear["note"],
        },
        "events": events,
    }

    limitations = [
        f"Small sample: {len(events)} events from one machine.",
        "Re-reads are a proxy for lost state: an agent may also re-ask the user or redo reasoning.",
        "Claude Code's own compaction summary quality is not ours; the replay reset "
        "model assumes a perfect summary.",
        f"/clear restarts: {clear['note']}.",
    ]
    report["limitations"] = limitations

    if events:
        report["saving_at_measured_f"] = _saving(
            root, since, aggregate["f"]["median"], aggregate["f"]["p75"]
        )
    else:
        report["saving_at_measured_f"] = {"available": False}
    return report


def _saving_lines(saving: dict[str, Any]) -> list[str]:
    if not saving.get("available", True):
        return ["No compaction events with a measurable drop; saving at measured f: n/a."]
    lines = [f"Saving at measured f (replay reset model, k={saving['k']}):"]
    for row in saving["rows"]:
        label = "median" if row["f"] == saving["f_median"] else "p75"
        lines.append(
            f"  f={row['f']:.4f} ({label}), S={row['summary_tokens']}, {row['variant']}: "
            f"saved {row['pct_saved']:.1f}% overall, "
            f"{row['pct_saved_affected']:.1f}% over affected sessions"
        )
    kill = saving["kill_threshold"]
    verdict = "met" if kill["met"] else "not met"
    lines.append(
        "Kill threshold for the checkpoint feature: saving at the measured median f "
        f"below {kill['pct']:.0f}%: {verdict} "
        f"(best {kill['best_pct_saved_at_median_f']:.1f}% overall)"
    )
    return lines


def format_refetch(report: dict[str, Any], dir_name: str = ".") -> str:
    """Render the refetch report as short plain text (counts and numbers only)."""
    sessions = report["sessions"]
    if sessions["parents"] == 0 and sessions["subagents"] == 0:
        return f"No transcripts found under {dir_name}."
    label = _SINCE_LABELS.get(str(report["since"]), str(report["since"]))
    skipped = report["events_skipped"]
    lines = [
        f"Claude Code refetch, {label}",
        f"Sessions scanned: {sessions['parents']:,} parents, "
        f"{sessions['subagents']:,} sub-agent files",
        f"Compaction events analysed: {report['events_analyzed']:,}, "
        f"skipped: {skipped['missing_usage'] + skipped['nonpositive_drop']:,} "
        f"(missing usage {skipped['missing_usage']:,}, "
        f"non-positive drop {skipped['nonpositive_drop']:,})",
        f"Dropped tokens D: median {report['d_tokens']['median']:,.0f}, "
        f"p25 {report['d_tokens']['p25']:,.0f}, p75 {report['d_tokens']['p75']:,.0f}",
        f"Re-fetch fraction f: median {report['f']['median']:.4f}, "
        f"p25 {report['f']['p25']:.4f}, p75 {report['f']['p75']:.4f}, "
        f"mean {report['f']['mean']:.4f}",
        f"Events with f above 0.5: {report['share_f_above_0_5'] * 100:.1f}% of "
        f"{report['events_analyzed']:,}",
        f"Window truncated at session end: {report['window_truncated']:,} events "
        f"(median turns in window {report['turns_in_window_median']:.0f})",
    ]
    if report["by_trigger"]:
        lines.append("By trigger (events, D median, f median, f mean, f above 0.5):")
        for trigger, st in report["by_trigger"].items():
            lines.append(
                f"  {trigger}: {st['events']:,} events, D {st['d_median']:,.0f}, "
                f"f {st['f_median']:.4f}, mean {st['f_mean']:.4f}, "
                f"above 0.5 {st['share_f_above_0_5'] * 100:.1f}%"
            )
    if report["by_source"]:
        lines.append("By source (events, f median):")
        for source, st in report["by_source"].items():
            lines.append(f"  {source}: {st['events']:,} events, f {st['f_median']:.4f}")
    lines.extend(_saving_lines(report["saving_at_measured_f"]))
    lines.append("Limitations:")
    for limitation in report["limitations"]:
        lines.append(f"  - {limitation}")
    lines.append("This report contains counts and numbers only; no paths, commands or text.")
    return "\n".join(lines)
