"""E-1: parent vs child cost, and re-discovery as a share of TOTAL session cost.

Gate 1 (``scripts/gate1_rediscovery.py``) reports re-discovery against child cost only.
That denominator overstates the product opportunity when most of a session's dollars are
spent by the parent. This script joins the same way, prices the parent too, and reports
re-discovery as a share of parent + children per session.

Aggregates only: counts, tokens, dollars and shares. No path, prompt, command or
transcript text is printed or written.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gate1_rediscovery as gate1  # noqa: E402

from cfxd.routing import (  # noqa: E402
    _DEFAULT_PRICES,
    _load_prices,
    _scan_turns,
    _turn_dollars,
)


def _parent_paths(root: Path) -> dict[str, Path]:
    """Session id -> parent transcript, matching Gate 1's duplicate-copy rule."""
    out: dict[str, Path] = {}
    for path in sorted(root.glob("*/*.jsonl")):
        out.setdefault(path.stem, path)
    return out


def parent_cost(path: Path, families: dict[str, Any]) -> tuple[float, bool]:
    """Parent dollars and whether any priced turn had an unknown model."""
    turns = _scan_turns(path, families)
    total = 0.0
    unpriced = False
    for turn in turns:
        fam = turn["family"]
        if fam is None:
            unpriced = True
            continue
        total += _turn_dollars(families[fam], turn["tokens"], turn["prompt"])
    return total, unpriced


def collect(root: Path, prices: dict[str, Any]) -> dict[str, Any]:
    """Per-session parent/child split and Gate 1 lower/upper, on total-cost shares."""
    data = gate1.collect(root, prices)
    families: dict[str, Any] = prices["families"]
    paths = _parent_paths(root)
    by_session: dict[str, list[dict[str, Any]]] = {}
    for child in data["children"]:
        by_session.setdefault(child["session"], []).append(child)

    rows: list[dict[str, Any]] = []
    excluded: Counter[str] = Counter()
    for sid, meta in data["sessions"].items():
        children = by_session.get(sid)
        if not children:
            excluded["no_joined_child"] += 1
            continue
        priced = [c for c in children if c["cost"] is not None]
        if not priced:
            excluded["unpriced_child"] += 1
            continue
        path = paths.get(sid)
        if path is None:
            excluded["no_parent_transcript"] += 1
            continue
        p_cost, p_unpriced = parent_cost(path, families)
        if p_unpriced:
            excluded["unpriced_parent"] += 1
            continue
        c_cost = sum(c["cost"] or 0.0 for c in priced)
        total = p_cost + c_cost
        if total <= 0:
            excluded["zero_cost"] += 1
            continue
        rows.append(
            {
                "session": sid,
                "parent_cost": p_cost,
                "child_cost": c_cost,
                "total": total,
                "lower": sum(c["lower"] or 0.0 for c in priced),
                "upper": sum(c["upper"] or 0.0 for c in priced),
                "children": len(priced),
                "unpriced_children": len(children) - len(priced),
                "parent_known": meta["read_before_first"],
            }
        )
    return {
        "rows": rows,
        "excluded": excluded,
        "sessions_with_children": len(data["sessions"]),
        "children": data["children"],
        "stats": data["stats"],
    }


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = sum(r["total"] for r in rows)
    parent = sum(r["parent_cost"] for r in rows)
    child = sum(r["child_cost"] for r in rows)
    lower = sum(r["lower"] for r in rows)
    upper = sum(r["upper"] for r in rows)
    return {
        "n": len(rows),
        "parent": parent,
        "child": child,
        "total": total,
        "children": sum(r["children"] for r in rows),
        "parent_share": gate1._share(parent, total),
        "child_share": gate1._share(child, total),
        "lower_share": gate1._share(lower, total),
        "upper_share": gate1._share(upper, total),
        "lower_of_child": gate1._share(lower, child),
        "upper_of_child": gate1._share(upper, child),
        "parent_q": gate1._q([r["parent_cost"] / r["total"] for r in rows]),
        "lower_q": gate1._q([r["lower"] / r["total"] for r in rows]),
        "upper_q": gate1._q([r["upper"] / r["total"] for r in rows]),
    }


def render(data: dict[str, Any]) -> str:
    rows = data["rows"]
    all_s = summarise(rows)
    ex = data["excluded"]
    kids = data["children"]
    priced_kids = [k for k in kids if k["cost"] is not None]
    out: list[str] = []
    w = out.append
    w("# E-1: the denominator — parent vs child cost and re-discovery's share of the session")
    w("")
    w("Generated by `scripts/denominator.py`. Aggregates only; no paths, prompts, commands")
    w("or transcript text. Dollars are API list-price equivalents, not a bill.")
    w("")
    w("## Sample")
    w("")
    w(
        f"- Parent sessions with sub-agent transcripts: {data['sessions_with_children']}; "
        f"in the denominator (a joined, priced child and a priced parent): {all_s['n']}"
    )
    w(
        f"- Excluded: {ex['no_joined_child']} with no joined child, "
        f"{ex['unpriced_child']} with no priced child, {ex['unpriced_parent']} with an "
        f"unpriced parent turn, {ex['zero_cost']} with zero cost"
    )
    w(
        f"- Joined children: {len(kids)} ({len(priced_kids)} priced, "
        f"{sum(r['unpriced_children'] for r in rows)} unpriced inside analysed sessions); "
        f"Gate 1 unjoinable calls: {data['stats']['unjoinable_no_agentid_in_result']} "
        f"without an agentId, {data['stats']['unjoinable_no_child_transcript']} without a "
        f"transcript"
    )
    w(
        f"- Total session cost {gate1._usd(all_s['total'])}: parent "
        f"{gate1._usd(all_s['parent'])} ({gate1._pct(all_s['parent_share'])}), children "
        f"{gate1._usd(all_s['child'])} ({gate1._pct(all_s['child_share'])}), pooled by dollars"
    )
    w("")
    w("## Headline")
    w("")
    w(
        f"- **Re-discovery as a share of total session cost: {gate1._pct(all_s['lower_share'])} "
        f"(lower) to {gate1._pct(all_s['upper_share'])} (upper)**, pooled by dollars."
    )
    w(
        f"- On Gate 1's child-cost denominator the same measures over these sessions are "
        f"{gate1._pct(all_s['lower_of_child'])} to {gate1._pct(all_s['upper_of_child'])}: "
        f"the parent is {gate1._pct(all_s['parent_share'])} of session cost, so the session "
        f"share is smaller by that factor."
    )
    w(
        f"- Per session (p25 / median / p75): parent share "
        f"{gate1._qs(all_s['parent_q'], gate1._pct)}; lower "
        f"{gate1._qs(all_s['lower_q'], gate1._pct)}; upper "
        f"{gate1._qs(all_s['upper_q'], gate1._pct)}"
    )
    w("")
    w("## By parent knowledge before the first spawn")
    w("")
    w(
        "| group | sessions | session cost | parent share | child share | lower | upper "
        "| lower (child cost) | upper (child cost) |"
    )
    w("|---|---|---|---|---|---|---|---|---|")
    groups = [
        ("all sessions", rows),
        ("parent knew 3 or more files", [r for r in rows if r["parent_known"] >= 3]),
        ("parent knew 1-2 files", [r for r in rows if 1 <= r["parent_known"] < 3]),
        ("parent knew 0 files", [r for r in rows if r["parent_known"] == 0]),
    ]
    for name, group in groups:
        if not group:
            continue
        s = summarise(group)
        w(
            f"| {name} | {s['n']} | {gate1._usd(s['total'])} | "
            f"{gate1._pct(s['parent_share'])} | {gate1._pct(s['child_share'])} | "
            f"{gate1._pct(s['lower_share'])} | {gate1._pct(s['upper_share'])} | "
            f"{gate1._pct(s['lower_of_child'])} | {gate1._pct(s['upper_of_child'])} |"
        )
    w("")
    w("## Method")
    w("")
    w(
        "- Join: the Gate 1 join, imported unchanged: parent `Agent` tool_use, then "
        "`toolUseResult.agentId`, then the child transcript."
    )
    w(
        "- Parent cost: every priced assistant turn in the parent transcript, usage counted "
        "once per `message.id` (`cfxd/usage_dedupe.py`), at the repo's list prices."
    )
    w(
        "- Child cost: every joined child's cost from Gate 1, which also excludes child "
        "messages that replay parent history and unpriced children."
    )
    w("- Total session cost = parent cost + joined child cost.")
    w(
        "- Lower / upper: Gate 1's attribution of a child's cost to redundant calls "
        "(re-read of a file the parent knew, re-run of a parent Bash command), divided here "
        "by total session cost instead of child cost."
    )
    w(
        "- Pooled shares are dollar-weighted across all sessions; per-session quartiles "
        "weight every session equally."
    )
    w(
        "- Parent knowledge: distinct paths the parent Read/Edited/Wrote on lines before "
        "its first `Agent` call (Bash commands are matched separately and are not counted "
        "as files)."
    )
    w("")
    w("## Caveats")
    w("")
    w(
        "- This is one person's sessions on one machine. Parent and child costs are list "
        "prices, not a bill."
    )
    w(
        "- Total session cost can be too low: children with no agentId link, a missing "
        "transcript or an unpriced model are excluded, so their spend is not in the "
        "denominator and the reported shares are if anything too high."
    )
    w(
        "- The parent transcript is priced from its own model turns only. Work done outside "
        "the transcript (other tools, the user's own reading) is not in the denominator."
    )
    w(
        "- Gate 1's parent knowledge is only what its Read/Edit/Write/Bash calls show; "
        "context from the user's prompt, compacted history or MCP tools is invisible, so "
        "re-discovery is undercounted and the lower/upper band may sit too low."
    )
    w(
        "- The lower figure ignores that a redundant result stays in context; the upper "
        "charges the whole turn and its carried results and estimates result tokens from "
        "characters. Treat the truth as between them."
    )
    w("- A re-read is not always waste: a child may need the exact bytes in context to edit.")
    w(
        "- Sessions are grouped by files the parent knew before its first spawn, so a "
        "session that spawned early and learned later is in the low group."
    )
    w("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="E-1 session-denominator report")
    ap.add_argument("--root", type=Path, default=Path.home() / ".claude" / "projects")
    ap.add_argument("--out", type=Path, default=_REPO / "docs" / "denominator.md")
    args = ap.parse_args(argv)
    prices = _load_prices(_DEFAULT_PRICES)
    text = render(collect(args.root, prices))
    sys.stdout.write(text + "\n")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
