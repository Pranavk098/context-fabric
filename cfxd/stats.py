"""``cfxd stats``: what fold and the hooks saved, from the daemon's own logs.

Reads only sizes, ids, and timestamps from ``folds.jsonl`` and ``events.jsonl`` —
never the tool output itself, which the logs never store anyway.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cfxd.bound import BOUND_LOG, TAIL_LINES
from cfxd.daemon import EVENTS_FILE, percentile
from cfxd.fold import FOLDS_LOG

_SINCE_LABELS = {"all": "all time", "7d": "last 7 days", "24h": "last 24 hours"}
_SINCE_DELTAS = {"7d": timedelta(days=7), "24h": timedelta(hours=24)}


def _read_objects(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    objects: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            objects.append(obj)
    return objects


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _in_window(ts: Any, cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    if not isinstance(ts, str):
        return False
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed >= cutoff


def _match_session(value: Any, session: str | None) -> bool:
    return session is None or value == session


def collect_stats(
    state_dir: Path,
    since: str = "all",
    session: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate fold and hook stats from ``state_dir``; missing/malformed logs are skipped."""
    if now is None:
        now = datetime.now(UTC)
    delta = _SINCE_DELTAS.get(since)
    cutoff = now - delta if delta is not None else None

    sessions: set[str] = set()
    fold_count = 0
    by_tool: dict[str, int] = {}
    bytes_in = 0
    bytes_out = 0

    for entry in _read_objects(state_dir / FOLDS_LOG):
        if not _match_session(entry.get("session_id"), session):
            continue
        if not _in_window(entry.get("ts"), cutoff):
            continue
        b_in = _as_int(entry.get("bytes_in"))
        b_out = _as_int(entry.get("bytes_out"))
        if b_in is None or b_out is None:
            continue
        sid = entry.get("session_id")
        if isinstance(sid, str):
            sessions.add(sid)
        fold_count += 1
        bytes_in += b_in
        bytes_out += b_out
        tool = entry.get("tool_name")
        if isinstance(tool, str):
            by_tool[tool] = by_tool.get(tool, 0) + 1

    latencies: list[float] = []
    errors = 0
    rejected = 0
    hook_count = 0
    for entry in _read_objects(state_dir / EVENTS_FILE):
        if not _match_session(entry.get("session_id"), session):
            continue
        if not _in_window(entry.get("ts"), cutoff):
            continue
        latency = _as_float(entry.get("latency_ms"))
        if latency is None:
            continue
        sid = entry.get("session_id")
        if isinstance(sid, str):
            sessions.add(sid)
        hook_count += 1
        latencies.append(latency)
        outcome = entry.get("outcome")
        if outcome == "error":
            errors += 1
        elif outcome == "rejected":
            rejected += 1

    saved = bytes_in - bytes_out
    percent = round(100.0 * saved / bytes_in) if bytes_in else 0
    tokens_estimated = saved // 4

    bounded = 0
    for entry in _read_objects(state_dir / BOUND_LOG):
        if entry.get("event") != "bound":
            continue
        if not _match_session(entry.get("session_id"), session):
            continue
        if not _in_window(entry.get("ts"), cutoff):
            continue
        bounded += 1

    return {
        "since": since,
        "sessions": len(sessions),
        "bounded": bounded,
        "folds": {
            "count": fold_count,
            "by_tool": dict(sorted(by_tool.items(), key=lambda kv: (-kv[1], kv[0]))),
        },
        "output": {
            "bytes_in": bytes_in,
            "bytes_out": bytes_out,
            "bytes_saved": saved,
            "tokens_estimated": tokens_estimated,
            "percent_saved": percent,
        },
        "hooks": {
            "count": hook_count,
            "p50_ms": round(percentile(latencies, 50), 1),
            "p95_ms": round(percentile(latencies, 95), 1),
            "errors": errors,
            "rejected": rejected,
        },
    }


def format_human(stats: dict[str, Any]) -> str:
    """Render the stats as the short plain-text report (``No folds yet.`` when empty)."""
    if not stats["folds"]["count"] and not stats["bounded"]:
        return "No folds yet."
    label = _SINCE_LABELS.get(str(stats["since"]), "all time")
    n = stats["sessions"]
    sess = f"{n} session" if n == 1 else f"{n} sessions"
    by_tool = ", ".join(f"{tool} {count:,}" for tool, count in stats["folds"]["by_tool"].items())
    out = stats["output"]
    hooks = stats["hooks"]
    lines = [
        f"Context Fabric, {label} ({sess})",
        f"{'Folds:':<12}{stats['folds']['count']:,}   ({by_tool})",
        (
            f"{'Output:':<12}{out['bytes_in']:,} bytes -> {out['bytes_out']:,} bytes"
            f"   (saved {out['bytes_saved']:,} bytes the model did not have to read,"
            f" about {out['tokens_estimated']:,} tokens, {out['percent_saved']}%)"
        ),
        (f"{'Bounded:':<12}{stats['bounded']:,}   Bash commands capped (tail {TAIL_LINES} lines)"),
        (
            f"{'Hook calls:':<12}{hooks['count']:,}   p50 {hooks['p50_ms']:.1f} ms"
            f"   p95 {hooks['p95_ms']:.1f} ms   errors {hooks['errors']}"
            f"   rejected {hooks['rejected']}"
        ),
    ]
    return "\n".join(lines)
