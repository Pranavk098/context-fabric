"""``cfxd week``: one shareable weekly summary built from the existing analyzers.

Calls ``audit``'s scanner, ``routing``, ``diet``, ``refetch`` and ``stats`` and
adds no new transcript rules of its own. The report holds counts, sizes and
model ids only, so testers can paste it back. Experimental: whether the
long-session pattern saves tokens is still unproven.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cfxd import __version__
from cfxd.audit import _SINCE_DELTAS, _discover, _scan, coverage
from cfxd.checkpoint import CHECKPOINT_LOG
from cfxd.config import read_config
from cfxd.diet import diet
from cfxd.dietapply import _read_records, _write_record
from cfxd.fsutil import ensure_dir
from cfxd.refetch import refetch
from cfxd.replay import _parse_ts
from cfxd.routing import _DEFAULT_PRICES, _load_prices, _scan_turns, _turn_dollars, routing
from cfxd.stats import _read_objects, collect_stats

_BUCKETS = (("2-10", 2, 10), ("11-30", 11, 30), ("31+", 31, None))
_SAFE = "Safe to paste: counts, sizes and model ids only."
_LONG_SESSION_LINE = (
    "Most of your cost is in long sessions; the checkpoint workflow targets exactly those."
)


def _since_key(days: int) -> str:
    """Register the window in audit's since table, which every analyzer reads."""
    key = f"{days}d"
    _SINCE_DELTAS[key] = timedelta(days=days)
    return key


def _meaning(report: dict[str, Any]) -> list[str]:
    """Up to four plain sentences chosen by fixed rules from the report's numbers."""
    if report["sessions"] == 0:
        return ["No sessions in this window yet, so there is nothing to optimise."]
    sentences: list[str] = []
    long = report["buckets"]["31+"]
    if long["sessions"] == 0:
        sentences.append(
            "No session ran past 30 assistant turns, so there is nothing to optimise yet."
        )
    elif long["share"] is not None and long["share"] > 0.5:
        sentences.append(_LONG_SESSION_LINE)
    switches = report["model_switches"]
    if switches["count"]:
        sentences.append(
            f"You switched models {switches['count']} "
            f"{'time' if switches['count'] == 1 else 'times'}, adding about "
            f"${switches['premium']:,.2f} in cache writes."
        )
    d = report["diet"]
    if d["unused_skills"] or d["unused_connectors"]:
        sentences.append(
            f"{d['unused_skills']} unused skills and {d['unused_connectors']} unused connectors "
            f"cost about {d['tokens_per_turn']:,.0f} est. tokens per turn before you type."
        )
    f = report["refetch"]["median_f"]
    if f is not None and f > 0.5:
        sentences.append(
            "After a compaction most of the dropped context is read again, "
            "so compaction saves little."
        )
    cache = report["cache_read_share_tokens"]
    if len(sentences) < 4 and cache is not None and cache >= 0.8:
        sentences.append(
            f"Cache reads are {cache * 100:.0f}% of tokens, so context is mostly reused, "
            "not re-sent."
        )
    if not sentences:
        sentences.append("Nothing stands out in these numbers; keep the current setup.")
    return sentences[:4]


def week(
    root: Path,
    days: int = 7,
    state_dir: Path | None = None,
    now: datetime | None = None,
    share: bool = False,
) -> dict[str, Any]:
    """Aggregate one weekly report from the existing analyzers; never guesses a number."""
    if now is None:
        now = datetime.now(UTC)
    since = _since_key(days)
    parents, _subs = _discover(root, now - timedelta(days=days))
    notes: dict[str, str] = {}

    try:
        families: dict[str, Any] | None = _load_prices(_DEFAULT_PRICES)["families"]
    except (OSError, ValueError, KeyError):
        families = None
        notes["dollars"] = "price table unavailable"

    # Per-session turn count (audit's scanner) and priced dollars (routing's helpers).
    per_session: list[tuple[int, float]] = []
    compactions = 0
    for path in parents:
        record = _scan(path, known=None)
        compactions += record["compactions"]
        dollars = 0.0
        if families is not None:
            for turn in _scan_turns(path, families):
                family = turn["family"]
                if family is not None:
                    dollars += _turn_dollars(families[family], turn["tokens"], turn["prompt"])
        per_session.append((int(record["turns"]), dollars))

    try:
        route: dict[str, Any] | None = routing(root, since, now=now)
    except (OSError, ValueError, KeyError):
        route = None
    if route is None:
        notes.setdefault("dollars", "price table unavailable")
    elif route["unpriced"]["turns"] > 0:
        notes["dollars"] = "unpriced model turns"
    priced = families is not None and route is not None and "dollars" not in notes

    total_dollars = round(sum(d for _t, d in per_session), 4) if priced else None
    buckets: dict[str, Any] = {}
    for label, low, high in _BUCKETS:
        members = [
            d for turns, d in per_session if turns >= low and (high is None or turns <= high)
        ]
        bucket_dollars: float | None = round(sum(members), 4) if priced else None
        share_ratio = 0.0
        if bucket_dollars is not None and total_dollars:
            share_ratio = round(bucket_dollars / total_dollars, 4)
        buckets[label] = {"sessions": len(members), "dollars": bucket_dollars, "share": share_ratio}

    tokens = route["totals"]["tokens"] if route is not None else None
    token_total = sum(tokens.values()) if tokens is not None else 0
    cache_read_share = (
        round(tokens["cache_read"] / token_total, 4) if token_total and tokens else None
    )
    switches = (
        {
            "count": route["model_switches"]["cold"]
            + route["model_switches"]["warm"]
            + route["model_switches"]["unpriced"],
            "premium": route["model_switches"]["premium_total"],
        }
        if route is not None
        else None
    )

    diet_report = diet(root, since, now=now)
    first_turn = diet_report["first_turn_tokens_median"]
    if first_turn is None:
        notes["first_turn"] = "no first-turn usage"
    unused_skills: int | None = None
    unused_connectors: int | None = None
    tokens_per_turn = diet_report["effect"]["per_turn_tokens"]
    if diet_report["sessions"] > 0 and diet_report["listing_not_found"] < diet_report["sessions"]:
        unused_skills = sum(g["count"] for g in diet_report["never_used"]["plugins"])
        unused_connectors = len(diet_report["never_used"]["connectors"])
    else:
        notes["diet"] = "no skill listing in transcripts"
        tokens_per_turn = None

    refetch_report = refetch(root, since, now=now)
    events = refetch_report["events_analyzed"]
    median_f = refetch_report["f"]["median"] if events else None
    if not events:
        notes["refetch"] = "no compaction events"

    receipts: dict[str, Any] = dict.fromkeys(
        (
            "folds",
            "bound_rewrites",
            "hook_calls",
            "hook_errors",
            "hook_rejected",
            "checkpoints",
            "cold_resumes",
        )
    )
    if state_dir is not None:
        stats = collect_stats(state_dir, "all", now=now)
        receipts["folds"] = stats["folds"]["count"]
        receipts["bound_rewrites"] = stats["bounded"]
        receipts["hook_calls"] = stats["hooks"]["count"]
        receipts["hook_errors"] = stats["hooks"]["errors"]
        receipts["hook_rejected"] = stats["hooks"]["rejected"]
        receipts["cold_resumes"] = stats["cold_resumes"]
    else:
        notes["receipts"] = "no state directory"
    notes["checkpoints"] = "not recorded by cfxd stats"
    hook_delivery = coverage(root, state_dir, days, now=now) if state_dir is not None else None

    report: dict[str, Any] = {
        "version": __version__,
        "days": days,
        "sessions": len(parents),
        "sessions_under_2_turns": sum(1 for turns, _d in per_session if turns < 2),
        "dollars_total": total_dollars,
        "median_dollars_per_session": (
            round(statistics.median(d for _t, d in per_session), 4)
            if priced and per_session
            else None
        ),
        "buckets": buckets,
        "cache_read_share_tokens": cache_read_share,
        "first_turn_context_median": first_turn,
        "compactions": compactions,
        "clear_restarts": refetch_report["clear_restarts"]["linked"],
        "model_switches": switches,
        "refetch": {"median_f": median_f, "events": events},
        "diet": {
            "unused_skills": unused_skills,
            "unused_connectors": unused_connectors,
            "tokens_per_turn": tokens_per_turn,
        },
        "receipts": receipts,
        "hook_delivery": hook_delivery,
        "notes": notes,
        "share": share,
        "safe_to_paste": _SAFE,
    }
    report["meaning"] = _meaning(report)
    return report


def _history_dir(state_dir: Path) -> Path:
    return state_dir / "history"


def _previous_snapshot(state_dir: Path, target: Path) -> Path | None:
    """Newest snapshot file other than the one about to be written (same-day overwrite)."""
    try:
        candidates = [p for p in _history_dir(state_dir).glob("week-*.json") if p != target]
    except OSError:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime_ns) if candidates else None


def _features_on(state_dir: Path, since: datetime | None) -> dict[str, Any]:
    """Switches from config, plus diet applies and checkpoint saves since the last snapshot."""
    config = read_config(state_dir)
    diet_applied = sum(
        1
        for _path, record in _read_records()
        if not record.get("undone_at")
        and any(
            entry.get("applied") is True
            for entry in (record.get("plugins") or [])
            if isinstance(entry, dict)
        )
    )
    checkpoint_saves = 0
    for entry in _read_objects(state_dir / CHECKPOINT_LOG):
        if entry.get("event") != "checkpoint_save" or entry.get("outcome") != "saved":
            continue
        ts = _parse_ts(entry.get("ts"))
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if since is not None and ts <= since:
            continue
        checkpoint_saves += 1
    return {
        "bound": config["bound"],
        "coach": config["coach"],
        "idle_notice": config["idle_notice"],
        "diet_applied": diet_applied,
        "checkpoint_saves": checkpoint_saves,
    }


def snapshot(
    state_dir: Path, report: dict[str, Any], now: datetime | None = None
) -> dict[str, Any]:
    """The numbers-only snapshot written by ``cfxd week --save``."""
    if now is None:
        now = datetime.now(UTC)
    target = _history_dir(state_dir) / f"week-{now.date().isoformat()}.json"
    previous = _previous_snapshot(state_dir, target)
    since = datetime.fromtimestamp(previous.stat().st_mtime, UTC) if previous is not None else None
    receipts = report["receipts"]
    delivery: float | None = None
    if receipts["hook_calls"]:
        delivered = receipts["hook_calls"] - receipts["hook_errors"] - receipts["hook_rejected"]
        delivery = round(delivered / receipts["hook_calls"], 4)
    return {
        "sessions": report["sessions"],
        "sessions_under_2_turns": report["sessions_under_2_turns"],
        "dollars_total": report["dollars_total"],
        "median_dollars_per_session": report["median_dollars_per_session"],
        "buckets": report["buckets"],
        "cache_read_share_tokens": report["cache_read_share_tokens"],
        "first_turn_context_median": report["first_turn_context_median"],
        "compactions": report["compactions"],
        "clear_restarts": report["clear_restarts"],
        "hook_delivery_rate": delivery,
        "features_on": _features_on(state_dir, since),
    }


def save_snapshot(state_dir: Path, report: dict[str, Any], now: datetime | None = None) -> Path:
    """Write one 0600 snapshot per day, atomically, overwriting the same day's file."""
    if now is None:
        now = datetime.now(UTC)
    path = _history_dir(state_dir) / f"week-{now.date().isoformat()}.json"
    ensure_dir(path.parent)
    _write_record(path, snapshot(state_dir, report, now))
    return path


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}" + ("" if count == 1 else "s")


def _value(value: float | None, reason: str, spec: str) -> str:
    return f"n/a ({reason})" if value is None else format(value, spec)


def format_week(report: dict[str, Any], share: bool = False) -> str:
    """Render the weekly report as plain text (counts, sizes and model ids only)."""
    notes: dict[str, str] = report["notes"]
    lines = [f"cfxd week {report['version']}, last {report['days']} days (experimental)"]
    if report["sessions"] == 0:
        lines.append("No transcripts found in this window.")
    else:
        dollar_reason = notes.get("dollars", "")
        lines.append(f"Sessions: {_plural(report['sessions'], 'parent session')}")
        lines.append("Sessions by length (assistant turns, share of API-equivalent dollars):")
        for label, bucket in report["buckets"].items():
            dollars = _value(bucket["dollars"], dollar_reason, ",.2f")
            if bucket["dollars"] is not None:
                dollars = "$" + dollars
            share_text = _value(bucket["share"], dollar_reason, ".4f")
            if bucket["share"] is not None:
                share_text = f"{bucket['share'] * 100:.1f}%"
            lines.append(
                f"  {label}: {_plural(bucket['sessions'], 'session')}, {dollars} "
                f"({share_text} of dollars)"
            )
        if report["sessions_under_2_turns"]:
            lines.append(f"  under 2 turns: {report['sessions_under_2_turns']:,} sessions")
        total = _value(report["dollars_total"], dollar_reason, ",.2f")
        if report["dollars_total"] is not None:
            total = "$" + total
        lines.append(f"Total API-equivalent dollars: {total}")
        cache = report["cache_read_share_tokens"]
        cache_text = "n/a (no tokens recorded)" if cache is None else f"{cache * 100:.1f}%"
        lines.append(f"Cache-read share of tokens: {cache_text}")
        lines.append(
            "First-turn context: median "
            + _value(report["first_turn_context_median"], notes.get("first_turn", ""), ",.0f")
            + " tokens"
        )
        lines.append(
            f"Compactions: {report['compactions']:,} boundaries, "
            f"{report['clear_restarts']:,} /clear restarts"
        )
        switches = report["model_switches"]
        if switches is None:
            lines.append(f"Model switches: n/a ({notes.get('dollars', '')})")
        else:
            lines.append(
                f"Model switches: {switches['count']:,}, premium ${switches['premium']:,.2f}"
            )
        lines.append(
            "Refetch after compaction: median f "
            + _value(report["refetch"]["median_f"], notes.get("refetch", ""), ".4f")
            + f" over {report['refetch']['events']:,} events"
        )
        d = report["diet"]
        if d["unused_skills"] is None:
            diet_line = f"Diet: n/a ({notes.get('diet', '')})"
        else:
            diet_line = (
                f"Diet: {d['unused_skills']:,} unused skills, "
                f"{d['unused_connectors']:,} unused connectors, "
                f"~{d['tokens_per_turn']:,.0f} est. tokens/turn (counts only)"
            )
        lines.append(diet_line)
        lines.extend(_receipt_lines(report))
        lines.append(_hook_delivery_line(report))
    lines.append("What this means:")
    lines.extend(f"  - {sentence}" for sentence in report["meaning"])
    if share:
        lines.append(_SAFE)
    return "\n".join(lines)


def _hook_delivery_line(report: dict[str, Any]) -> str:
    data = report.get("hook_delivery")
    if data is None:
        return "Hook delivery: n/a (no state directory)"
    delivery = data["delivery"]
    if delivery is None:
        return f"Hook delivery: n/a ({data['na_reason']})"
    return (
        f"Hook delivery: {delivery['overall'] * 100:.0f}% of tool calls observed "
        f"({data['sessions_compared']} sessions)"
    )


def _receipt_lines(report: dict[str, Any]) -> list[str]:
    r = report["receipts"]
    notes = report["notes"]
    if r["folds"] is None:
        lines = [f"Plugin receipts (cfxd stats): n/a ({notes.get('receipts', '')})"]
    else:
        checkpoints = _value(None, notes["checkpoints"], ",.0f")
        lines = [
            f"Plugin receipts (cfxd stats, all time): {r['folds']:,} fold rewrites, "
            f"{r['bound_rewrites']:,} bound rewrites, {r['hook_calls']:,} hook calls "
            f"({r['hook_errors']:,} errors, {r['hook_rejected']:,} rejected), "
            f"checkpoints {checkpoints}"
        ]
    cold = r["cold_resumes"]
    if cold is None:
        lines.append(
            f"Cold resumes (from receipts): n/a ({notes.get('receipts', 'no state directory')})"
        )
    elif not cold["receipts"]:
        lines.append("Cold resumes (from receipts): n/a (no resume receipts)")
    else:
        lines.append(f"Cold resumes (from receipts): {cold['count']:,}, about ${cold['usd']:,.2f}")
    return lines
