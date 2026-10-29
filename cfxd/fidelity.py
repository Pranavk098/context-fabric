"""``cfxd fidelity``: how faithful a compaction summary is to the session.

Counts and numbers only — never paths, names, summary text or hashes. The
summary text is read in memory to extract path-like tokens and then discarded;
paths are normalised and hashed in memory, and the report contains aggregate
numbers only (same canary rule as ``cfxd audit`` and ``cfxd refetch``).

For each compaction boundary whose summary text is present, the summary's
path-like mentions are compared with the files the session read or edited
before the boundary (recall) and with every path-like token seen earlier in
tool inputs or results (precision). An unseen mention may be an invention or a
file the summary knew from elsewhere; the number is a proxy, not a verdict.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cfxd.audit import _SINCE_DELTAS, _SINCE_LABELS, _discover, _iter_objects, _texts
from cfxd.daemon import percentile
from cfxd.refetch import _FILE_TOOLS, _ctx, _trigger

_MAX_TOKEN = 200
_RECENT = 20
_STRIP = "\"'`()[]{}<>,;:!?"
_EXT_RE = re.compile(r"\.[A-Za-z]{1,6}$")
_TRIGGERS = ("manual", "auto", "unknown")
_SKIP_REASONS = ("no_summary_text", "no_held_files")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _norm(path: str) -> str:
    """Lower-case, forward slashes, no trailing slash (comparison form only)."""
    return path.replace("\\", "/").lower().rstrip("/")


def _base(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _suffixes(path: str) -> list[str]:
    """Separator-aligned suffixes of a normalised path (basename included)."""
    parts = path.split("/")
    return ["/".join(parts[i:]) for i in range(len(parts)) if parts[i]]


def _path_tokens(text: str) -> list[str]:
    """Normalised path-like tokens: a separator or a 1-6 letter extension."""
    out: list[str] = []
    for raw in text.split():
        token = raw.strip(_STRIP).rstrip(".")
        token = token.strip(_STRIP)
        if not token or len(token) > _MAX_TOKEN:
            continue
        if "/" not in token and "\\" not in token and not _EXT_RE.search(token):
            continue
        norm = _norm(token)
        if norm:
            out.append(norm)
    return out


def _strings(value: Any) -> list[str]:
    """Every string inside a tool input (nested dicts and lists)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_strings(item))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_strings(item))
        return out
    return []


def _summary_text(obj: dict[str, Any]) -> str:
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return " ".join(_texts(content))


class _Seen:
    """Hashed path-like tokens seen in earlier tool inputs and results."""

    def __init__(self) -> None:
        self.hashes: set[str] = set()

    def add(self, text: str) -> None:
        for token in _path_tokens(text):
            self.hashes.add(_hash(token))
            for suffix in _suffixes(token):
                self.hashes.add(_hash(suffix))

    def matches(self, mention: str) -> bool:
        return _hash(mention) in self.hashes or _hash(_base(mention)) in self.hashes


class _Held:
    """Distinct files touched before the boundary, in last-touch order."""

    def __init__(self) -> None:
        self.order: dict[str, int] = {}
        self.seq = 0

    def add(self, path: str) -> None:
        norm = _norm(path)
        if not norm:
            return
        self.seq += 1
        self.order[norm] = self.seq

    def paths(self) -> list[str]:
        return list(self.order)

    def recent(self) -> list[str]:
        ranked = sorted(self.order.items(), key=lambda kv: -kv[1])[:_RECENT]
        return [path for path, _ in ranked]


def _matches(path: str, full: set[str], base: set[str]) -> bool:
    if _hash(path) in full:
        return True
    if any(_hash(suffix) in full for suffix in _suffixes(path)):
        return True
    return _hash(_base(path)) in base


def _event(
    text: str, held: list[str], recent: list[str], seen: _Seen, trigger: str
) -> dict[str, Any]:
    mentions = sorted(set(_path_tokens(text)))
    full = {_hash(mention) for mention in mentions}
    base = {_hash(_base(mention)) for mention in mentions}
    matched = sum(1 for path in held if _matches(path, full, base))
    recent_matched = sum(1 for path in recent if _matches(path, full, base))
    unseen = sum(1 for mention in mentions if not seen.matches(mention))
    n = len(held)
    return {
        "trigger": trigger,
        "summary_tokens": round(len(text.encode("utf-8")) / 4.0, 1),
        "mentions": len(mentions),
        "held_files": n,
        "recall": round(matched / n, 4),
        "recall_recent20": round(recent_matched / len(recent), 4),
        "precision": round((len(mentions) - unseen) / len(mentions), 4) if mentions else 1.0,
        "unseen_mentions": unseen,
        "checkpoint_recall_bound": round(min(_RECENT, n) / n, 4),
        "checkpoint_precision": 1.0,
    }


def _scan(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    events: list[dict[str, Any]] = []
    skipped = {reason: 0 for reason in _SKIP_REASONS}
    held = _Held()
    seen = _Seen()
    pending_trigger = "unknown"

    for obj in _iter_objects(path):
        is_summary = "isCompactSummary" in obj
        if is_summary or obj.get("subtype") == "compact_boundary":
            if not is_summary:
                # The boundary line carries the trigger; the summary line does not.
                pending_trigger = _trigger(obj)
                skipped["no_summary_text"] += 1
                continue
            trigger = _trigger(obj)
            if trigger == "unknown":
                trigger = pending_trigger
            pending_trigger = "unknown"
            text = _summary_text(obj)
            if not text.strip():
                skipped["no_summary_text"] += 1
                continue
            paths = held.paths()
            if not paths:
                skipped["no_held_files"] += 1
                continue
            events.append(_event(text, paths, held.recent(), seen, trigger))
            continue

        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []

        if obj.get("type") == "assistant":
            usage = msg.get("usage") if isinstance(msg, dict) else None
            has_usage = _ctx(usage if isinstance(usage, dict) else None) is not None
            for block in blocks:
                if block.get("type") != "tool_use":
                    continue
                inp = block.get("input")
                if not isinstance(inp, dict):
                    continue
                for text in _strings(inp):
                    seen.add(text)
                if not has_usage:
                    continue
                name = block.get("name")
                file_path = inp.get("file_path")
                if name in _FILE_TOOLS and isinstance(file_path, str):
                    held.add(file_path)

        for block in blocks:
            if block.get("type") == "tool_result":
                for text in _texts(block.get("content")):
                    seen.add(text)

    return events, skipped


def _dist(values: list[float], digits: int = 4) -> dict[str, float]:
    return {
        "median": round(percentile(values, 50), digits),
        "p25": round(percentile(values, 25), digits),
        "p75": round(percentile(values, 75), digits),
        "mean": round(sum(values) / len(values), digits) if values else 0.0,
    }


def _by_trigger(events: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for trigger in _TRIGGERS:
        group = [e for e in events if e["trigger"] == trigger]
        if not group:
            continue
        out[trigger] = {
            "events": len(group),
            "recall_median": round(percentile([float(e["recall"]) for e in group], 50), 4),
            "recall_recent20_median": round(
                percentile([float(e["recall_recent20"]) for e in group], 50), 4
            ),
            "precision_median": round(percentile([float(e["precision"]) for e in group], 50), 4),
            "summary_tokens_median": round(
                percentile([float(e["summary_tokens"]) for e in group], 50), 1
            ),
            "share_with_unseen_mentions": round(
                sum(1 for e in group if e["unseen_mentions"] > 0) / len(group), 4
            ),
        }
    return out


def _meaning(report: dict[str, Any]) -> list[str]:
    if report["events_analyzed"] == 0:
        return [
            "No compaction summary with held files was analysed, so there is nothing to compare."
        ]
    recall = report["recall"]
    recent = report["recall_recent20"]
    unseen = report["unseen_mentions"]
    share = report["share_with_unseen_mentions"]
    bound = report["checkpoint"]["recall_bound"]
    if share > 0:
        unseen_sentence = (
            f"{share * 100:.1f}% of events named at least one path never seen before the boundary "
            f"(median {unseen['median']:.0f} per event)."
        )
    else:
        unseen_sentence = "No event named a path that had not appeared before the boundary."
    return [
        f"Summaries matched a median {recall['median'] * 100:.1f}% of all held files and "
        f"{recent['median'] * 100:.1f}% of the 20 most recently touched.",
        unseen_sentence,
        f"A 20-file checkpoint cap allows at most a median {bound['median'] * 100:.1f}% recall; "
        f"its listed files are observed, so its precision is 1.0 by construction.",
    ]


def fidelity(root: Path, since: str = "30d", now: datetime | None = None) -> dict[str, Any]:
    """Aggregate summary fidelity over every transcript under ``root``."""
    if now is None:
        now = datetime.now(UTC)
    delta = _SINCE_DELTAS.get(since)
    cutoff = now - delta if delta is not None else None
    parents, subs = _discover(root, cutoff)

    events: list[dict[str, Any]] = []
    skipped = {reason: 0 for reason in _SKIP_REASONS}
    for path in [*parents, *(p for p, _ in subs)]:
        found, file_skipped = _scan(path)
        events.extend(found)
        for reason, count in file_skipped.items():
            skipped[reason] += count

    unseen = [float(e["unseen_mentions"]) for e in events]
    with_unseen = sum(1 for e in events if e["unseen_mentions"] > 0)
    report: dict[str, Any] = {
        "since": since,
        "sessions": {"parents": len(parents), "subagents": len(subs)},
        "events_analyzed": len(events),
        "events_skipped": skipped,
        "summary_tokens": _dist([float(e["summary_tokens"]) for e in events], 1),
        "recall": _dist([float(e["recall"]) for e in events]),
        "recall_recent20": _dist([float(e["recall_recent20"]) for e in events]),
        "precision": _dist([float(e["precision"]) for e in events]),
        "unseen_mentions": _dist(unseen),
        "events_with_unseen_mentions": with_unseen,
        "share_with_unseen_mentions": round(with_unseen / len(events), 4) if events else 0.0,
        "events_with_no_mentions": sum(1 for e in events if e["mentions"] == 0),
        "checkpoint": {
            "recall_bound": _dist([float(e["checkpoint_recall_bound"]) for e in events]),
            "precision": 1.0,
        },
        "by_trigger": _by_trigger(events),
        "events": events,
    }
    report["what_this_means"] = _meaning(report)
    report["limitations"] = [
        "One machine: these numbers describe the transcripts under this root only.",
        "Path mentions are a proxy for fidelity; a summary can retain useful state "
        "without naming files.",
        "A named path may be right without having been touched, and a touched file may be "
        "left out.",
        "Summary quality is Claude Code's, not ours.",
    ]
    return report


def _line(label: str, dist: dict[str, float], digits: int = 4) -> str:
    return (
        f"{label}: median {dist['median']:.{digits}f}, p25 {dist['p25']:.{digits}f}, "
        f"p75 {dist['p75']:.{digits}f}, mean {dist['mean']:.{digits}f}"
    )


def format_fidelity(report: dict[str, Any], dir_name: str = ".") -> str:
    """Render the fidelity report as short plain text (counts and numbers only)."""
    sessions = report["sessions"]
    if sessions["parents"] == 0 and sessions["subagents"] == 0:
        return f"No transcripts found under {dir_name}."
    label = _SINCE_LABELS.get(str(report["since"]), str(report["since"]))
    skipped = report["events_skipped"]
    n = report["events_analyzed"]
    lines = [
        f"Claude Code compaction-summary fidelity, {label}",
        f"Sessions scanned: {sessions['parents']:,} parents, "
        f"{sessions['subagents']:,} sub-agent files",
        f"Compaction boundaries analysed: {n:,}, skipped: "
        f"{skipped['no_summary_text'] + skipped['no_held_files']:,} "
        f"(no summary text {skipped['no_summary_text']:,}, "
        f"no held files {skipped['no_held_files']:,})",
        _line("Summary size (tokens)", report["summary_tokens"], 1),
        _line("Recall of held files", report["recall"]),
        _line("Recall of the 20 most recent files", report["recall_recent20"]),
        _line("Precision of named paths", report["precision"]),
        _line("Unseen mentions per event", report["unseen_mentions"], 1),
        f"Events with unseen mentions above 0: "
        f"{report['share_with_unseen_mentions'] * 100:.1f}% of {n:,}",
        f"Events naming no path: {report['events_with_no_mentions']:,}",
        "Checkpoint comparison (20-file cap, observed files):",
        "  " + _line("recall bound", report["checkpoint"]["recall_bound"]),
        f"  precision: {report['checkpoint']['precision']:.4f} by construction",
    ]
    if report["by_trigger"]:
        lines.append("By trigger (events, recall median, recent-20 median, precision median):")
        for trigger, st in report["by_trigger"].items():
            lines.append(
                f"  {trigger}: {st['events']:,} events, recall {st['recall_median']:.4f}, "
                f"recent-20 {st['recall_recent20_median']:.4f}, "
                f"precision {st['precision_median']:.4f}, "
                f"unseen above 0 {st['share_with_unseen_mentions'] * 100:.1f}%"
            )
    lines.append("What this means:")
    for sentence in report["what_this_means"]:
        lines.append(f"  - {sentence}")
    lines.append("Limitations:")
    for limitation in report["limitations"]:
        lines.append(f"  - {limitation}")
    lines.append(
        "This report contains counts and numbers only; no paths, names, summary text or hashes."
    )
    return "\n".join(lines)
