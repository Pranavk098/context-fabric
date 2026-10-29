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
from cfxd.checkpoint import CHECKPOINT_LOG
from cfxd.daemon import percentile
from cfxd.dietapply import _read_records
from cfxd.refetch import _CLEAR_GAP, _scan, refetch
from cfxd.replay import _parse_ts, replay
from cfxd.routing import _DEFAULT_PRICES, _load_prices, _scan_turns
from cfxd.stats import _read_objects, collect_stats

_HEADER = (
    "No saving is proven. Lines are labelled OBSERVED, ESTIMATED or PROVEN; "
    "PROVEN needs the pre-registered paired test (evals/plugin_ab/PROTOCOL_V2.md)."
)
_SAFE = "Safe to paste: counts, sizes and model ids only."
_NOT_COUNTERFACTUAL = "Other changes also move this number; it is not a counterfactual."
_ASSUMPTION = (
    "assumes you would have kept using the old session for as many turns, "
    "and that nothing was lost by restarting"
)
_CEILING_LABEL = "offline ceiling, assumes a perfect handoff; not a forecast"
_NOT_LINKABLE = "n/a (cannot link restores to sessions)"
_MIN_AFTER = 3
_RESET_K = 40
_SUMMARY_TOKENS = 2000
_MTOK = 1_000_000.0


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
        applies.append({"at": at, "plugins": len(entries)})
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


def _receipts(state_dir: Path, cutoff: datetime) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for entry in _read_objects(state_dir / CHECKPOINT_LOG):
        ts = _parse_ts(entry.get("ts"))
        if ts is None or ts < cutoff:
            continue
        events.append(entry)
    return events


def _link_new(
    sessions: list[dict[str, Any]], session_id: Any, ts: datetime
) -> dict[str, Any] | None:
    """The restored session: by receipt session id, else the only one starting within 10 min."""
    if isinstance(session_id, str) and session_id:
        for session in sessions:
            if session["session_id"] == session_id:
                return session
    candidates = [s for s in sessions if ts <= s["first_ts"] <= ts + _CLEAR_GAP]
    return candidates[0] if len(candidates) == 1 else None


def _link_old(
    sessions: list[dict[str, Any]], new: dict[str, Any], ts: datetime
) -> dict[str, Any] | None:
    """The session that ended just before the restore, in the same project directory."""
    candidates = [
        s
        for s in sessions
        if s is not new
        and s["dir"] == new["dir"]
        and s["last_ts"] is not None
        and s["last_ts"] <= ts
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s["last_ts"])


def _cache_read_price(
    path: Path, families: dict[str, Any] | None
) -> tuple[str | None, float | None]:
    """Model id and cache-read price per Mtok of the session's first priced turn."""
    if families is None:
        return None, None
    for turn in _scan_turns(path, families):
        family = turn["family"]
        if family is not None:
            return str(family), float(families[family]["cache_read"])
    return None, None


def _checkpoint(
    sessions: list[dict[str, Any]],
    events: list[dict[str, Any]],
    families: dict[str, Any] | None,
) -> dict[str, Any]:
    saves = sum(
        1 for e in events if e.get("event") == "checkpoint_save" and e.get("outcome") == "saved"
    )
    restores = [
        e
        for e in events
        if e.get("event") == "checkpoint_restore" and e.get("outcome") == "restored"
    ]
    estimates: list[dict[str, Any]] = []
    for event in restores:
        ts = _parse_ts(event.get("ts"))
        if ts is None:
            continue
        new = _link_new(sessions, event.get("session_id"), ts)
        if new is None:
            continue
        old = _link_old(sessions, new, ts)
        if old is None:
            continue
        model, price = _cache_read_price(new["path"], families)
        if price is None:
            continue
        avoided = max(0, int(old["last_ctx"]) - int(new["first_ctx"]))
        dollars = avoided * int(new["turns"]) * price / _MTOK
        estimates.append(
            {
                "old_ctx": old["last_ctx"],
                "new_ctx": new["first_ctx"],
                "avoided_tokens": avoided,
                "turns": new["turns"],
                "model": model,
                "cache_read_per_mtok": price,
                "dollars": round(dollars, 6),
            }
        )
    return {
        "saves": saves,
        "restores": len(restores),
        "linked": len(estimates),
        "tokens_avoided": sum(e["avoided_tokens"] for e in estimates),
        "dollars_avoided": round(sum(float(e["dollars"]) for e in estimates), 6),
        "estimates": estimates,
        "assumption": _ASSUMPTION,
    }


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

    try:
        families: dict[str, Any] | None = _load_prices(_DEFAULT_PRICES)["families"]
    except (OSError, ValueError, KeyError):
        families = None

    refetch_report = refetch(root, since, now=now)
    f_median = refetch_report["f"]["median"] if refetch_report["events_analyzed"] else None

    if state_dir is not None:
        stats = collect_stats(state_dir, "all", now=now)
        notices: dict[str, Any] | None = {
            "bound_rewrites": stats["bounded"],
            "coach_nudges": stats["nudges"],
            "idle_notices": stats["cold_resumes"]["receipts"],
            "cold_resumes": stats["cold_resumes"]["count"],
        }
        checkpoint = _checkpoint(sessions, _receipts(state_dir, cutoff), families)
    else:
        notices = None
        checkpoint = _checkpoint(sessions, [], families)

    return {
        "version": __version__,
        "days": days,
        "sessions": len(parents),
        "sessions_scanned": len(sessions),
        "header": _HEADER,
        "diet": _diet(sessions),
        "checkpoint": checkpoint,
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
                f"after (n={entry['after_n']}), delta {float(entry['delta']):+,.0f} tokens"
            )
        lines.append(f"  {_NOT_COUNTERFACTUAL}")
    return lines


def _checkpoint_lines(report: dict[str, Any]) -> list[str]:
    c = report["checkpoint"]
    lines = [
        f"Checkpoint (OBSERVED counts, ESTIMATED effect): {c['saves']:,} saves, "
        f"{c['restores']:,} restores"
    ]
    if c["restores"] == 0:
        lines.append("  no restore receipts; nothing to estimate")
        return lines
    if c["linked"] == 0:
        lines.append(f"  {_NOT_LINKABLE}")
        return lines
    for index, e in enumerate(c["estimates"], start=1):
        lines.append(
            f"  restore {index}: {float(e['old_ctx']):,.0f} -> {float(e['new_ctx']):,.0f} tokens, "
            f"avoided {e['avoided_tokens']:,} tokens x {e['turns']:,} turns at "
            f"{e['model']} ${float(e['cache_read_per_mtok']):.2f}/Mtok cache read = "
            f"${float(e['dollars']):.4f}"
        )
    lines.append(f"  avoided {c['tokens_avoided']:,} tokens, about ${c['dollars_avoided']:,.4f}")
    lines.append(f"  {_ASSUMPTION}")
    if c["linked"] < c["restores"]:
        lines.append(f"  {c['restores'] - c['linked']} restore(s) not linkable to a session")
    return lines


def _notices_lines(report: dict[str, Any]) -> list[str]:
    notices = report["notices"]
    if notices is None:
        return ["Bound / coach / idle notice (OBSERVED): n/a (no state directory)"]
    return [
        "Bound / coach / idle notice (OBSERVED, counts from receipts only, no dollars): "
        f"bound rewrites {notices['bound_rewrites']:,}, "
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
    lines.extend(_checkpoint_lines(report))
    lines.extend(_notices_lines(report))
    lines.extend(_ceiling_lines(report))
    lines.append(f"PROVEN: {report['proven']}")
    if share:
        lines.append(_SAFE)
    return "\n".join(lines)
