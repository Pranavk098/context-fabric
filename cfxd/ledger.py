"""``cfxd ledger``: what the plugin did and what changed, one evidence tier per line.

OBSERVED lines are counts, sizes or before/after medians taken from the user's own
transcripts and the plugin's receipts. ESTIMATED lines are a documented formula with a
stated assumption. PROVEN would need the pre-registered paired test in
``evals/plugin_ab/PROTOCOL_V2.md``; none has run yet, so it is always "none yet".

Reuses ``audit``/``refetch`` scanners, ``replay``'s reset model, ``routing``'s price table
and ``stats``' receipt readers. Counts, sizes and model ids only — never paths, project
names, prompts or session ids.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cfxd import __version__
from cfxd.audit import _SINCE_DELTAS, _discover
from cfxd.daemon import percentile
from cfxd.dietapply import _read_records
from cfxd.refetch import _scan, refetch
from cfxd.replay import _parse_ts, replay
from cfxd.stats import collect_stats

_HEADER = (
    "No saving is proven. Lines are labelled OBSERVED, ESTIMATED or PROVEN; "
    "PROVEN needs the pre-registered paired test (evals/plugin_ab/PROTOCOL_V2.md)."
)
_SAFE = "Safe to paste: counts, sizes and model ids only."
_NOT_COUNTERFACTUAL = "Other changes also move this number; it is not a counterfactual."
_CEILING_LABEL = "offline ceiling, assumes a perfect handoff; not a forecast"
_MIN_AFTER = 3
_RESET_K = 40
_SUMMARY_TOKENS = 2000


def _session(path: Path) -> dict[str, Any] | None:
    """One parent session: start/end time, first/last context, turn count (refetch's scan)."""
    scan = _scan(path)
    if not scan.turns or scan.first_ts is None:
        return None
    return {
        "path": path,
        "dir": path.parent,
        "session_id": path.stem,
        "first_ts": scan.first_ts,
        "last_ts": scan.last_ts,
        "first_ctx": scan.turns[0].ctx,
        "last_ctx": scan.turns[-1].ctx,
        "turns": len(scan.turns),
    }


def _median(values: list[int]) -> float | None:
    return round(percentile([float(v) for v in values], 50), 1) if values else None


def _applied_records() -> list[dict[str, Any]]:
    """Diet apply records that changed something and were not undone, oldest first."""
    applies: list[dict[str, Any]] = []
    for _path, record in reversed(_read_records()):
        if record.get("undone_at"):
            continue
        plugins = record.get("plugins")
        entries = [e for e in plugins if isinstance(e, dict)] if isinstance(plugins, list) else []
        if not any(e.get("applied") is True for e in entries):
            continue
        at = _parse_ts(record.get("created_at"))
        if at is None:
            continue
        estimates = [
            e["est_tokens_per_turn"]
            for e in entries
            if isinstance(e.get("est_tokens_per_turn"), (int, float))
        ]
        applies.append(
            {
                "at": at,
                "plugins": len(entries),
                "est_tokens_per_turn": (
                    round(sum(float(v) for v in estimates), 1) if estimates else None
                ),
            }
        )
    return applies


def _diet(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """First-turn context medians before vs after each apply; OBSERVED counts, no causality."""
    applies: list[dict[str, Any]] = []
    for record in _applied_records():
        at: datetime = record["at"]
        before = [s["first_ctx"] for s in sessions if s["first_ts"] < at]
        after = [s["first_ctx"] for s in sessions if s["first_ts"] >= at]
        entry: dict[str, Any] = {
            "at": at.isoformat(),
            "plugins": record["plugins"],
            "est_tokens_per_turn": record["est_tokens_per_turn"],
            "before_n": len(before),
            "after_n": len(after),
            "before_median": _median(before),
            "after_median": _median(after),
            "delta": None,
            "status": "measured",
        }
        if len(after) < _MIN_AFTER:
            entry["status"] = f"not enough sessions after the apply yet (n={len(after)})"
        elif not before:
            entry["status"] = "no sessions before the apply in this window"
        else:
            entry["delta"] = round(float(entry["after_median"]) - float(entry["before_median"]), 1)
        applies.append(entry)
    return {"applies": applies}


def _ceiling(root: Path, since: str, f_median: float | None, now: datetime) -> dict[str, Any]:
    """Replay's own reset model at k=40, S=2000 and the measured median re-fetch fraction."""
    ceiling: dict[str, Any] = {
        "label": _CEILING_LABEL,
        "k": _RESET_K,
        "summary_tokens": _SUMMARY_TOKENS,
        "f": f_median,
        "pct_bounded": None,
    }
    if f_median is None:
        return ceiling
    model = replay(
        root,
        since,
        ks=(),
        batches=(),
        reset_ks=(_RESET_K,),
        summary_tokens=(_SUMMARY_TOKENS,),
        refetch=(f_median,),
        now=now,
    )
    for policy in model["reset_policies"]:
        for variant in policy["variants"]:
            if variant["variant"] == "upper_bound" and variant["f"] == f_median:
                ceiling["pct_bounded"] = variant["pct_saved"]
    return ceiling


def ledger(
    root: Path,
    days: int = 30,
    state_dir: Path | None = None,
    now: datetime | None = None,
    share: bool = False,
) -> dict[str, Any]:
    """One ledger over the window; every number comes from an existing analyzer or formula."""
    if now is None:
        now = datetime.now(UTC)
    since = f"{days}d"
    _SINCE_DELTAS[since] = timedelta(days=days)
    cutoff = now - timedelta(days=days)
    parents, _subs = _discover(root, cutoff)
    sessions = [s for p in parents if (s := _session(p)) is not None]

    refetch_report = refetch(root, since, now=now)
    f_median = refetch_report["f"]["median"] if refetch_report["events_analyzed"] else None

    if state_dir is not None:
        stats = collect_stats(state_dir, "all", now=now)
        notices: dict[str, Any] | None = {
            "coach_nudges": stats["nudges"],
            "idle_notices": stats["cold_resumes"]["receipts"],
            "cold_resumes": stats["cold_resumes"]["count"],
        }
    else:
        notices = None

    return {
        "version": __version__,
        "days": days,
        "sessions": len(parents),
        "sessions_scanned": len(sessions),
        "header": _HEADER,
        "diet": _diet(sessions),
        "notices": notices,
        "ceiling": _ceiling(root, since, f_median, now),
        "proven": "none yet",
        "safe_to_paste": _SAFE,
        "share": share,
    }


def _tokens(value: Any) -> str:
    return f"{float(value):,.0f}"


def _diet_lines(report: dict[str, Any]) -> list[str]:
    applies = report["diet"]["applies"]
    if not applies:
        return ["Diet (OBSERVED): n/a (no applied diet records)"]
    lines: list[str] = []
    for entry in applies:
        lines.append(
            f"Diet (OBSERVED): apply {str(entry['at'])[:10]}, {entry['plugins']} plugins disabled"
        )
        if entry["status"] != "measured":
            lines.append(f"  {entry['status']}")
        else:
            lines.append(
                f"  first-turn context median: {_tokens(entry['before_median'])} tokens "
                f"before (n={entry['before_n']}), {_tokens(entry['after_median'])} tokens "
                f"after (n={entry['after_n']}), delta {float(entry['delta']):+,.0f} tokens "
                "(OBSERVED)"
            )
        estimate = entry["est_tokens_per_turn"]
        if estimate is None:
            lines.append("  ESTIMATED tokens/turn removed: n/a (record has no per-plugin estimate)")
        else:
            lines.append(
                f"  ESTIMATED ~{float(estimate):,.0f} tokens/turn removed at apply "
                "(sum of the record's per-plugin estimates)"
            )
        lines.append(f"  {_NOT_COUNTERFACTUAL}")
    return lines


def _notices_lines(report: dict[str, Any]) -> list[str]:
    notices = report["notices"]
    if notices is None:
        return ["Coach / idle notice (OBSERVED): n/a (no state directory)"]
    return [
        "Coach / idle notice (OBSERVED, counts from receipts only, no dollars): "
        f"coach nudges {notices['coach_nudges']:,}, "
        f"idle notices {notices['idle_notices']:,} "
        f"(cold resumes {notices['cold_resumes']:,})"
    ]


def _ceiling_lines(report: dict[str, Any]) -> list[str]:
    c = report["ceiling"]
    label = f"Restart ceiling (ESTIMATED, {_CEILING_LABEL})"
    if c["pct_bounded"] is None:
        return [f"{label}: n/a (no compaction events to measure a re-fetch fraction)"]
    return [
        f"{label}: k={c['k']}, S={c['summary_tokens']}, f={float(c['f']):.4f} measured median; "
        f"bounds {float(c['pct_bounded']):.1f}% of your API-equivalent dollars"
    ]


def format_ledger(report: dict[str, Any], share: bool = False) -> str:
    """Render the ledger as plain text: counts, sizes and model ids only."""
    lines = [
        f"cfxd ledger {report['version']}, last {report['days']} days (experimental)",
        report["header"],
    ]
    if report["sessions"] == 0:
        lines.append("No transcripts found in this window.")
    lines.extend(_diet_lines(report))
    lines.extend(_notices_lines(report))
    lines.extend(_ceiling_lines(report))
    lines.append(f"PROVEN: {report['proven']}")
    if share:
        lines.append(_SAFE)
    return "\n".join(lines)
