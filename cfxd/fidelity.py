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
from cfxd.bound import test_key_any
from cfxd.daemon import percentile
from cfxd.refetch import _FILE_TOOLS, _ctx, _trigger

_MAX_TOKEN = 200
_RECENT = 20
_STRIP = "\"'`()[]{}<>,;:!?"
# Frozen v1 (docs/FIDELITY-METRICS.md): one allowlist, one definition.
_EXTENSIONS = frozenset(
    "py js jsx ts tsx json md toml yaml yml sh go rs java kt c cc cpp h hpp css "
    "scss html sql txt cfg ini lock xml ipynb rb php cs swift".split()
)
_PATH_PREFIXES = ("/", "~/", "./", "../")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_PASS_CLAIMS = (
    "tests pass",
    "tests passed",
    "all tests pass",
    "tests passing",
    "all passing",
    "all green",
    "0 failed",
    "no failures",
)
_EDIT_TOOLS = _FILE_TOOLS - {"Read"}
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


def _ext(token: str) -> str:
    """Lower-case extension of the token's last segment ("" when absent)."""
    last = re.split(r"[\\/]", token)[-1]
    stem, _, ext = last.rpartition(".")
    return ext.lower() if stem else ""


def _is_path(token: str) -> bool:
    """Frozen v1 strict path mention: anchored path, separator+extension, or bare file."""
    if "://" in token or "@" in token or not re.search(r"[A-Za-z]", token):
        return False
    if token.startswith(_PATH_PREFIXES) or _DRIVE_RE.match(token):
        return True
    if _ext(token) not in _EXTENSIONS:
        return False
    if "/" in token or "\\" in token:
        return True
    return len(token.rsplit(".", 1)[0]) >= 3


def _path_tokens(text: str) -> list[str]:
    """Normalised strict path tokens (see docs/FIDELITY-METRICS.md)."""
    out: list[str] = []
    for raw in text.split():
        token = raw.strip(_STRIP).rstrip(".")
        token = token.strip(_STRIP)
        if not token or len(token) > _MAX_TOKEN or not _is_path(token):
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


def _test_failed(block: dict[str, Any], texts: list[str]) -> bool:
    """True when a test-runner result shows a non-zero exit, error, interrupt or timeout."""
    if block.get("is_error"):
        return True
    joined = " ".join(texts).strip()
    lower = joined.lower()
    if lower.startswith(("interrupted", "timed out", "command timed out")):
        return True
    match = re.match(r"exit code (\d+)", joined, re.I)
    return bool(match and int(match.group(1)) != 0)


def _event(
    text: str,
    held: list[str],
    recent: list[str],
    seen: _Seen,
    trigger: str,
    edited: set[str],
    test_ok: bool | None,
) -> dict[str, Any]:
    mentions = sorted(set(_path_tokens(text)))
    full = {_hash(mention) for mention in mentions}
    base = {_hash(_base(mention)) for mention in mentions}
    matched = sum(1 for path in held if _matches(path, full, base))
    recent_matched = sum(1 for path in recent if _matches(path, full, base))
    unseen = sum(1 for mention in mentions if not seen.matches(mention))
    dropped = sum(1 for path in edited if not _matches(path, full, base))
    claim = any(phrase in text.lower() for phrase in _PASS_CLAIMS)
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
        "edited_files": len(edited),
        "dropped_edits": dropped,
        "pass_claim": claim,
        "unsupported_pass_claim": claim and test_ok is not True,
        "checkpoint_recall_bound": round(min(_RECENT, n) / n, 4),
        "checkpoint_precision": 1.0,
    }


def _scan(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    events: list[dict[str, Any]] = []
    skipped = {reason: 0 for reason in _SKIP_REASONS}
    held = _Held()
    seen = _Seen()
    edited: set[str] = set()
    pending_tests: set[str] = set()
    test_ok: bool | None = None
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
            events.append(_event(text, paths, held.recent(), seen, trigger, edited, test_ok))
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
                name = block.get("name")
                file_path = inp.get("file_path")
                if name in _EDIT_TOOLS and isinstance(file_path, str):
                    norm = _norm(file_path)
                    if norm:
                        edited.add(norm)
                command = inp.get("command")
                if name == "Bash" and isinstance(command, str):
                    test_id = block.get("id")
                    if test_key_any(command) is not None and isinstance(test_id, str):
                        # Unfinished or failed until its result says otherwise.
                        pending_tests.add(test_id)
                        test_ok = False
                if not has_usage:
                    continue
                if name in _FILE_TOOLS and isinstance(file_path, str):
                    held.add(file_path)

        for block in blocks:
            if block.get("type") != "tool_result":
                continue
            texts = _texts(block.get("content"))
            for text in texts:
                seen.add(text)
            test_id = block.get("tool_use_id")
            if isinstance(test_id, str) and test_id in pending_tests:
                pending_tests.discard(test_id)
                test_ok = not _test_failed(block, texts)

    return events, skipped


def _dist(values: list[float], digits: int = 4) -> dict[str, float]:
    return {
        "median": round(percentile(values, 50), digits),
        "p25": round(percentile(values, 25), digits),
        "p75": round(percentile(values, 75), digits),
        "mean": round(sum(values) / len(values), digits) if values else 0.0,
    }


def _frozen(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Frozen metrics v1 (docs/FIDELITY-METRICS.md); counts and rates only."""
    mentions = sum(e["mentions"] for e in events)
    unsupported = sum(e["unseen_mentions"] for e in events)
    with_edits = [e for e in events if e["edited_files"]]
    edited = sum(e["edited_files"] for e in with_edits)
    dropped = sum(e["dropped_edits"] for e in with_edits)
    claims = [e for e in events if e["pass_claim"]]
    return {
        "version": 1,
        "M1": {
            "mentions": mentions,
            "unsupported": unsupported,
            "rate": round(unsupported / mentions, 4) if mentions else 0.0,
            "per_event": _dist([float(e["unseen_mentions"]) for e in events]),
        },
        "M2": {
            "edited_files": edited,
            "dropped": dropped,
            "rate": round(dropped / edited, 4) if edited else 0.0,
            "events_with_edits": len(with_edits),
            "events_excluded": len(events) - len(with_edits),
        },
        "M3": {
            "events_with_pass_claim": len(claims),
            "unsupported": sum(1 for e in claims if e["unsupported_pass_claim"]),
        },
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
        "frozen_metrics": _frozen(events),
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
    frozen = report["frozen_metrics"]
    m1 = frozen["M1"]
    m2 = frozen["M2"]
    m3 = frozen["M3"]
    lines = [
        f"Claude Code compaction-summary fidelity, {label}",
        f"Sessions scanned: {sessions['parents']:,} parents, "
        f"{sessions['subagents']:,} sub-agent files",
        f"Compaction boundaries analysed: {n:,}, skipped: "
        f"{skipped['no_summary_text'] + skipped['no_held_files']:,} "
        f"(no summary text {skipped['no_summary_text']:,}, "
        f"no held files {skipped['no_held_files']:,})",
        "Frozen metrics v1",
        f"  M1 unsupported path rate: {m1['rate']:.4f} "
        f"({m1['unsupported']:,} of {m1['mentions']:,} mentions; "
        f"per event median {m1['per_event']['median']:.1f})",
        f"  M2 dropped edit rate: {m2['rate']:.4f} "
        f"({m2['dropped']:,} of {m2['edited_files']:,} edited files; "
        f"{m2['events_excluded']:,} events with no edits excluded)",
        f"  M3 unsupported pass claims: {m3['unsupported']:,} of "
        f"{m3['events_with_pass_claim']:,} events with a pass claim",
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
