"""``cfxd checkup``: one numbers-only report a tester can run and paste back.

Fixed order, nothing else: diet (the numeric ``--share`` view), compaction-summary
fidelity (frozen metrics M1/M2/M3), then sub-agent re-discovery (``handoff-stats``
numbers). Each section reuses the shipped analyzer; a section with no data prints one
line saying so and the report continues. Exit status is always 0.

Privacy: counts and percentages only. No skill, connector, model, session, agent or
file names, no paths, prompts or text, and the report ends with the safe-to-paste line.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cfxd.audit import DEFAULT_ROOT
from cfxd.config import read_config
from cfxd.daemon import resolve_state_dir
from cfxd.diet import _numeric, diet
from cfxd.fidelity import fidelity
from cfxd.handoff_stats import collect, summarise
from cfxd.routing import _DEFAULT_PRICES, _load_prices

_SAFE = "safe to paste: counts and percentages only"
_ESTIMATE = "an estimate, not a bill"


def _diet_section(root: Path, since: str, now: datetime) -> tuple[dict[str, Any] | None, list[str]]:
    report = _numeric(diet(root, since, now=now))
    if report["sessions"] == 0 or report["listing_not_found"] >= report["sessions"]:
        return None, ["Diet: no data (no skill or connector listing in this window)."]
    unused_skills = sum(g["count"] for g in report["never_used"]["plugins"])
    unused_connectors = len(report["never_used"]["connectors"])
    data = {
        "sessions": report["sessions"],
        "unused_skills": unused_skills,
        "unused_connectors": unused_connectors,
        "prefix_tokens_estimated_median": report["prefix_tokens_estimated_median"],
        "tokens_per_turn": report["effect"]["per_turn_tokens"],
        "estimate": _ESTIMATE,
    }
    lines = [
        f"Diet (sessions: {report['sessions']:,}; {_ESTIMATE}):",
        f"  unused skills: {unused_skills:,}; unused connectors: {unused_connectors:,}",
        f"  setup prefix: ~{report['prefix_tokens_estimated_median']:,.0f} est. tokens; "
        f"unused prefix: ~{report['effect']['per_turn_tokens']:,.0f} est. tokens per turn",
    ]
    return data, lines


def _fidelity_section(
    root: Path, since: str, now: datetime
) -> tuple[dict[str, Any] | None, list[str]]:
    report = fidelity(root, since, now=now)
    if report["events_analyzed"] == 0:
        return None, ["Fidelity: no data (no compaction summary with held files in this window)."]
    frozen = report["frozen_metrics"]
    m1, m2, m3 = frozen["M1"], frozen["M2"], frozen["M3"]
    data = {"events_analyzed": report["events_analyzed"], "M1": m1, "M2": m2, "M3": m3}
    lines = [
        f"Fidelity (frozen metrics v1, compactions: {report['events_analyzed']:,}):",
        f"  M1 unsupported path rate: {m1['rate']:.4f} "
        f"({m1['unsupported']:,} of {m1['mentions']:,} mentions)",
        f"  M2 dropped edit rate: {m2['rate']:.4f} "
        f"({m2['dropped']:,} of {m2['edited_files']:,} edited files)",
        f"  M3 unsupported pass claims: {m3['unsupported']:,} of "
        f"{m3['events_with_pass_claim']:,} events with a pass claim",
    ]
    return data, lines


def _subagent_section(root: Path) -> tuple[dict[str, Any] | None, list[str]]:
    report = summarise(collect(root, _load_prices(_DEFAULT_PRICES)))
    if report["children"] == 0:
        return None, ["Sub-agents: no data (no child transcript joined to a parent here)."]
    brief_on = read_config(resolve_state_dir(None)).get("brief_v1", False)
    cost = report["cost_share"]
    data = {
        "brief_on": brief_on,
        "sessions": report["sessions"],
        "children": report["children"],
        "families": report["families"],
        "cost_share": cost,
    }
    lines = [
        "Sub-agents (cfxd handoff-stats numbers):",
        f"  Brief on: {'yes' if brief_on else 'no'}; sessions with child transcripts: "
        f"{report['sessions']:,}; joined children: {report['children']:,}",
        "  by child model family (children, parent knew 3+ files, re-reads of those files, "
        "re-discovery share):",
    ]
    for row in report["families"]:
        share = (
            "n/a" if row["rediscovery_share"] is None else f"{100 * row['rediscovery_share']:.1f}%"
        )
        lines.append(
            f"    {row['family']}: {row['children']:,}, {row['parent_knew_3']:,}, "
            f"{row['rereads']:,} of {row['read_calls']:,} read calls, {share}"
        )
    if cost["lower"] is None:
        lines.append("  re-discovery cost share: n/a (no priced session)")
    else:
        lines.append(
            "  re-discovery cost share of total session cost: "
            f"{100 * cost['lower']:.1f}% (lower) to {100 * cost['upper']:.1f}% (upper)"
        )
    return data, lines


def checkup(root: Path, since: str = "30d", now: datetime | None = None) -> dict[str, Any]:
    """Run the three analyzers in order; each returns data or one no-data line."""
    if now is None:
        now = datetime.now(UTC)
    report: dict[str, Any] = {
        "since": since,
        "diet": None,
        "fidelity": None,
        "sub_agents": None,
        "no_data": [],
        "lines": [],
        "safe_to_paste": "counts and percentages only",
    }
    for key, (data, lines) in (
        ("diet", _diet_section(root, since, now)),
        ("fidelity", _fidelity_section(root, since, now)),
        ("sub_agents", _subagent_section(root)),
    ):
        report[key] = data
        report["lines"].extend(lines)
        if data is None:
            report["no_data"].append(key)
    return report


def format_checkup(report: dict[str, Any]) -> str:
    """Header, the three sections in order, then the exact safe-to-paste line."""
    lines = [f"cfxd checkup (numbers only, {report['since']})"]
    lines.extend(report["lines"])
    lines.append(_SAFE)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cfxd checkup",
        description="one numbers-only tester report: diet, fidelity, sub-agents",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"transcripts root (default: {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--since",
        choices=("7d", "30d", "all"),
        default="30d",
        help="time window by file mtime (default: 30d)",
    )
    parser.add_argument("--json", action="store_true", help="emit a single JSON object")
    args = parser.parse_args(argv)
    report = checkup(args.root, args.since)
    if args.json:
        print(json.dumps({key: value for key, value in report.items() if key != "lines"}))
    else:
        print(format_checkup(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
