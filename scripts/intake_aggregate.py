"""P10 intake aggregation: per-developer medians and ranges, consent enforced here.

Reads the accepted log from ``scripts/intake_validate.py`` and answers P10's paper questions
with counts, one row per developer. Two rules are enforced in code, not by intention:

* A record whose ``consent_aggregate_publish`` is not true is excluded from every table and
  counted as product-fixes only.
* Below three consented developers there is no paper table: the script prints
  "qualitative round only" and stops. The floor is fixed before tester week.

No limit share exists for any tier (D-3: no official token-denominated plan window), so the
report leads with limit-weighted new tokens (``limit-weighted/1``) and says so. Aggregates
only: no paths, prompts, commands or free text.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

FLOOR = 3  # the minimum credible dataset, named before tester week
FRICTION = ("install", "comprehension", "trust", "performance")
# Fields that are counts: a developer's median is rounded to a whole number.
COUNTS = ("sessions", "turns", "children", "brief_sessions", "carry_sessions", "elisions")


def load(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Records from an accepted.jsonl; returns ``(records, unreadable_line_count)``."""
    records: list[dict[str, Any]] = []
    unreadable = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            share, feedback = record["share"], record["feedback"]
        except (ValueError, KeyError, TypeError):
            unreadable += 1
            continue
        if not isinstance(share, dict) or not isinstance(feedback, dict):
            unreadable += 1
            continue
        records.append(record)
    return records, unreadable


def _values(records: list[dict[str, Any]], section: str, key: str) -> list[float]:
    return [
        float(record[section][key])
        for record in records
        if isinstance(record[section][key], (int, float))
    ]


def _developer_row(developer: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    """One row per developer: the per-field median over that developer's consented records."""

    def value(section: str, key: str) -> float | int | None:
        numbers = _values(records, section, key)
        if not numbers:
            return None
        median = statistics.median(numbers)
        return int(round(median)) if key in COUNTS else median

    def friction(name: str) -> int:
        return int(round(statistics.median([r["feedback"]["friction"][name] for r in records])))

    return {
        "developer": developer,
        # A developer reports one OS family; sorted() keeps a mixed input deterministic.
        "os": sorted({record["feedback"]["os_family"] for record in records})[0],
        "submissions": len(records),
        "sessions": value("share", "sessions"),
        "turns": value("share", "turns"),
        "children": value("share", "children"),
        "new_token_equivalents": value("share", "new_token_equivalents"),
        "rediscovery_share_pct": value("share", "rediscovery_share_pct"),
        "brief_sessions": value("feedback", "brief_sessions"),
        "carry_sessions": value("feedback", "carry_sessions"),
        "elisions": value("share", "elisions"),
        "friction": {name: friction(name) for name in FRICTION},
    }


def aggregate(records: list[dict[str, Any]], unreadable: int = 0) -> dict[str, Any]:
    """Consent partition plus one row per consented developer.

    A developer who submits twice is one developer, not two: their records collapse to one row
    of per-field medians, and the floor counts distinct developers.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    excluded = 0
    for record in records:
        if record["share"].get("consent_aggregate_publish") is not True:
            excluded += 1
            continue
        grouped.setdefault(record["feedback"]["developer"], []).append(record)
    rows = [_developer_row(developer, grouped[developer]) for developer in sorted(grouped)]
    return {
        "rows": rows,
        "excluded": excluded,
        "unreadable": unreadable,
        "read": len(records),
        "consented_records": len(records) - excluded,
    }


def median_range(values: list[float]) -> tuple[float, float, float] | None:
    if not values:
        return None
    return statistics.median(values), min(values), max(values)


def _numbers(rows: list[dict[str, Any]], key: str) -> list[float]:
    return [float(row[key]) for row in rows if isinstance(row[key], (int, float))]


def _tokens(value: Any) -> str:
    return "n/a" if value is None else f"{value:,.2f}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def _compare(label: str, rows: list[dict[str, Any]], key: str, fmt: Any) -> str:
    values = _numbers(rows, key)
    stats = median_range(values)
    if stats is None:
        return f"- {label}: n/a (no payload carried a value)"
    median, low, high = stats
    return (
        f"- {label}: median {fmt(median)}, range {fmt(low)} to {fmt(high)} "
        f"({len(values)} developers)"
    )


def render(data: dict[str, Any]) -> str:
    rows = data["rows"]
    out: list[str] = []
    w = out.append
    w("# Tester-week intake: P10 aggregate report")
    w("")
    w("Generated by `scripts/intake_aggregate.py` from validated intake payloads. Counts and")
    w("shares only: no paths, prompts, commands or free text.")
    w("Every figure is *observed*: testers' own share output, not a paired run.")
    w("No saving is proven.")
    w("")
    w("## Consent")
    w("")
    w(f"- Records read: {data['read']}")
    w(f"- Consented developers (one row each): {len(rows)}")
    w(
        "- Consented to aggregate-and-publish counts "
        f"(records in the tables below): {data['consented_records']}"
    )
    w(f"- Product fixes only, excluded from every table: {data['excluded']}")
    if data["unreadable"]:
        w(f"- Unreadable lines skipped: {data['unreadable']}")
    w("")
    w("## Limit metric")
    w("")
    w(
        "- No limit share is available for any tier (D-3: Anthropic publishes no official "
        "token-denominated plan window), so this report leads with limit-weighted new tokens "
        "(`limit-weighted/1`)."
    )
    w("")
    if len(rows) < FLOOR:
        w("## Dataset floor")
        w("")
        w(f"- Consented developers: {len(rows)}; the floor fixed before tester week is {FLOOR}.")
        w("- qualitative round only")
        w("")
        return "\n".join(out)
    w("## Per developer")
    w("")
    w(
        "| developer | OS | submissions | sessions | turns | children "
        "| limit-weighted new tokens | re-discovery share | brief sessions | carry sessions "
        "| elisions |"
    )
    w("|---|---|---|---|---|---|---|---|---|---|---|")
    for row in rows:
        w(
            f"| {row['developer']} | {row['os']} | {row['submissions']} | {row['sessions']} | "
            f"{row['turns']} | {row['children']} | {_tokens(row['new_token_equivalents'])} | "
            f"{_pct(row['rediscovery_share_pct'])} | {row['brief_sessions']} | "
            f"{row['carry_sessions']} | {row['elisions']} |"
        )
    w("")
    w("## Across developers")
    w("")
    w(
        "One row per developer, each row the median of that developer's submissions; the range "
        "is across developers, not across sessions or submissions."
    )
    w("")
    w(
        _compare(
            "Re-discovery share of total session cost (E-1 replication, observed)",
            rows,
            "rediscovery_share_pct",
            _pct,
        )
    )
    w(_compare("Limit-weighted new tokens (observed)", rows, "new_token_equivalents", _tokens))
    w(_compare("Sessions (observed)", rows, "sessions", lambda v: f"{v:.0f}"))
    w("")
    w("## Friction counts (per developer)")
    w("")
    w("Each developer's counts are the median over their submissions; the column sums them.")
    w("")
    w("| category | occurrences |")
    w("|---|---|")
    totals: Counter[str] = Counter()
    for row in rows:
        totals.update({name: int(row["friction"].get(name, 0)) for name in FRICTION})
    for name in FRICTION:
        w(f"| {name} | {totals[name]} |")
    w("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate validated P10 intake payloads.")
    parser.add_argument(
        "--in",
        dest="source",
        type=Path,
        default=Path("intake") / "accepted.jsonl",
        help="accepted log from intake_validate.py (default: ./intake/accepted.jsonl)",
    )
    parser.add_argument("--out", type=Path, help="also write the report to this file")
    args = parser.parse_args(argv)
    if not args.source.is_file():
        print(f"intake: {args.source} not found; run intake_validate.py first", file=sys.stderr)
        return 2
    records, unreadable = load(args.source)
    text = render(aggregate(records, unreadable))
    sys.stdout.write(text + "\n")
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
