"""E-6: per-hook latency percentiles from the daemon's own receipts.

The daemon writes one ``events.jsonl`` line per hook call (``cfxd/daemon.py``):
event name, tool name, latency, outcome and byte counts. This script reads only
those fields from ``events.jsonl`` and its rotated predecessor, groups them by hook
event, and reports p50/p95/p99 plus the share of calls over the handler deadline.

``latency_ms`` is server-side: request accepted to response written. The daemon uses
150 ms for ordinary hooks and 1300 ms when ``tool_name`` is ``Agent``; a call that
misses its deadline returns ``{}`` (fail open). Aggregates only — no paths, session
ids, payloads or tool names are printed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from cfxd.daemon import (
    AGENT_HANDLER_TIMEOUT_MS,
    EVENTS_FILE,
    HANDLER_TIMEOUT_MS,
    percentile,
)
from cfxd.hookclient import resolve_state_dir

_ROTATED = EVENTS_FILE + ".1"


def _read_entries(state_dir: Path) -> tuple[list[dict[str, Any]], int]:
    """Valid receipt objects from the live and rotated event logs, plus malformed lines."""
    entries: list[dict[str, Any]] = []
    malformed = 0
    for name in (EVENTS_FILE, _ROTATED):
        try:
            text = (state_dir / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if isinstance(obj, dict):
                entries.append(obj)
            else:
                malformed += 1
    return entries, malformed


def _latency(entry: dict[str, Any]) -> float | None:
    value = entry.get("latency_ms")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _timeout(entry: dict[str, Any]) -> float:
    return AGENT_HANDLER_TIMEOUT_MS if entry.get("tool_name") == "Agent" else HANDLER_TIMEOUT_MS


def _summary(latencies: list[float], over: int, errors: int, rejected: int) -> dict[str, Any]:
    count = len(latencies)
    return {
        "count": count,
        "p50_ms": round(percentile(latencies, 50), 2),
        "p95_ms": round(percentile(latencies, 95), 2),
        "p99_ms": round(percentile(latencies, 99), 2),
        "over_timeout": over,
        "over_timeout_share": round(over / count, 4) if count else 0.0,
        "errors": errors,
        "rejected": rejected,
    }


def summarise(entries: list[dict[str, Any]], malformed: int = 0) -> dict[str, Any]:
    """Per-event latency percentiles and timeout shares from receipt objects."""
    by_event: dict[str, list[float]] = {}
    over_by_event: dict[str, int] = {}
    errors_by_event: dict[str, int] = {}
    rejected_by_event: dict[str, int] = {}
    all_latencies: list[float] = []
    over_total = 0
    errors_total = 0
    rejected_total = 0
    skipped = 0

    for entry in entries:
        latency = _latency(entry)
        if latency is None:
            skipped += 1
            continue
        event = entry.get("event")
        name = event if isinstance(event, str) and event else "unknown"
        by_event.setdefault(name, []).append(latency)
        all_latencies.append(latency)
        if latency > _timeout(entry):
            over_by_event[name] = over_by_event.get(name, 0) + 1
            over_total += 1
        outcome = entry.get("outcome")
        if outcome == "error":
            errors_by_event[name] = errors_by_event.get(name, 0) + 1
            errors_total += 1
        elif outcome == "rejected":
            rejected_by_event[name] = rejected_by_event.get(name, 0) + 1
            rejected_total += 1

    events = [
        {
            "event": name,
            **_summary(
                latencies,
                over_by_event.get(name, 0),
                errors_by_event.get(name, 0),
                rejected_by_event.get(name, 0),
            ),
        }
        for name, latencies in sorted(by_event.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]
    return {
        "receipts": len(all_latencies),
        "malformed": malformed,
        "skipped": skipped,
        "timeouts_ms": {"handler": HANDLER_TIMEOUT_MS, "agent": AGENT_HANDLER_TIMEOUT_MS},
        "events": events,
        "total": _summary(all_latencies, over_total, errors_total, rejected_total),
    }


def render(report: dict[str, Any]) -> str:
    """Plain-text report: one row per hook event and the timeout share."""
    total = report["total"]
    if not total["count"]:
        return "No hook receipts."
    lines = [
        f"Hook latency: {total['count']:,} daemon receipts "
        f"(handler timeout {report['timeouts_ms']['handler']:.0f} ms, "
        f"subagent {report['timeouts_ms']['agent']:.0f} ms)",
        f"{'event':<16}{'count':>7}{'p50 ms':>9}{'p95 ms':>9}{'p99 ms':>9}{'over':>7}",
    ]
    for row in report["events"]:
        lines.append(
            f"{row['event']:<16}{row['count']:>7,}{row['p50_ms']:>9.2f}"
            f"{row['p95_ms']:>9.2f}{row['p99_ms']:>9.2f}{row['over_timeout']:>7,}"
        )
    over = total["over_timeout"]
    share = total["over_timeout_share"] * 100.0
    lines.append(f"Over handler timeout: {over:,} of {total['count']:,} ({share:.1f}%)")
    lines.append(f"Errors {total['errors']:,}, rejected {total['rejected']:,}")
    if report["malformed"]:
        lines.append(f"Skipped malformed lines: {report['malformed']:,}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Per-hook latency percentiles (E-6).")
    parser.add_argument("--state-dir", help="cfxd state dir (default: CFX_HOME or ~/.cfx/daemon)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)
    state_dir = resolve_state_dir(args.state_dir)
    entries, malformed = _read_entries(state_dir)
    report = summarise(entries, malformed)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
