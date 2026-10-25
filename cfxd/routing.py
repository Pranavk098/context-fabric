"""``cfxd routing``: where tokens and API-equivalent dollars go, by model and role.

Reads only model ids and usage numbers from local Claude Code transcripts — never
paths, prompts, commands, tool names or message text. The report contains model
ids, roles, counts, tokens and dollars only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cfxd.audit import (
    _SINCE_DELTAS,
    _SINCE_LABELS,
    _as_int,
    _discover,
    _iter_objects,
    _weighted,
)
from cfxd.daemon import percentile

_DEFAULT_PRICES = Path(__file__).with_name("data") / "prices.json"

_ROLES = ("parent", "subagent")
_TOKEN_KINDS = ("input", "cache_creation_5m", "cache_creation_1h", "cache_read", "output")

# Claude Code writes a placeholder model id on synthetic assistant turns (titles,
# summaries). Those turns have no billable model, so they are skipped.
_SYNTHETIC_MODEL = "<synthetic>"
_HAIKU_100K = 100_000
_MTOK = 1_000_000.0

# A cold model switch must rewrite a context above this many tokens where at
# least half of it is cache creation (the new model's first cache write).
_COLD_CONTEXT = 20_000

_DISCLAIMER = (
    "Dollars are API-list-price equivalents, not a bill. Subscription users pay no "
    "per-token dollars; this is a common currency for relative weight."
)
_LIMITS = (
    "Same token counts assumed: a cheaper model may take more turns, fail more, or need re-runs.",
    "Quality is not measured here.",
    "This is an upper bound on the price effect only.",
    "Model-switch premiums are API-list-price equivalents, not a bill.",
)

_FAMILIES_KEY = "families"


def _load_prices(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    families: dict[str, Any] = {}
    for entry in data.get("models", []):
        if not isinstance(entry, dict):
            continue
        fam = entry.get("family")
        if isinstance(fam, str) and fam:
            families[fam] = entry
    data[_FAMILIES_KEY] = families
    return data


def _normalize(model: str) -> str:
    """``claude-sonnet-5-5`` / ``claude-haiku-4-5-20251001`` -> ``sonnet-5-5`` / ``haiku-4-5``."""
    name = model.strip().lower()
    if name.startswith("claude-"):
        name = name[len("claude-") :]
    parts = [p for p in name.split("-") if not (p.isdigit() and len(p) >= 4)]
    return "-".join(parts)


def _match_family(norm: str, families: dict[str, Any]) -> str | None:
    """Longest-prefix family match (``sonnet-5-5`` beats ``sonnet-5``)."""
    best: str | None = None
    for fam in families:
        if norm.startswith(fam) and (best is None or len(fam) > len(best)):
            best = fam
    return best


def _tokens(usage: dict[str, Any]) -> dict[str, int]:
    cc = usage.get("cache_creation")
    cc5m = 0
    cc1h = 0
    if isinstance(cc, dict) and (
        "ephemeral_5m_input_tokens" in cc or "ephemeral_1h_input_tokens" in cc
    ):
        cc5m = _as_int(cc.get("ephemeral_5m_input_tokens")) or 0
        cc1h = _as_int(cc.get("ephemeral_1h_input_tokens")) or 0
    else:
        # No 5m/1h split in this transcript: price the whole creation at the 1h rate.
        cc1h = _as_int(usage.get("cache_creation_input_tokens")) or 0
    return {
        "input": _as_int(usage.get("input_tokens")) or 0,
        "cache_creation_5m": cc5m,
        "cache_creation_1h": cc1h,
        "cache_read": _as_int(usage.get("cache_read_input_tokens")) or 0,
        "output": _as_int(usage.get("output_tokens")) or 0,
    }


def _prompt(tokens: dict[str, int]) -> int:
    return (
        tokens["input"]
        + tokens["cache_creation_5m"]
        + tokens["cache_creation_1h"]
        + tokens["cache_read"]
    )


def _price_for(entry: dict[str, Any], prompt: int) -> dict[str, Any]:
    over = entry.get("over_100k")
    if isinstance(over, dict) and prompt > _HAIKU_100K:
        return over
    return entry


def _turn_dollars(entry: dict[str, Any], tokens: dict[str, int], prompt: int) -> float:
    price = _price_for(entry, prompt)
    total = (
        tokens["input"] * float(price["input"])
        + tokens["cache_creation_5m"] * float(price["cache_write_5m"])
        + tokens["cache_creation_1h"] * float(price["cache_write_1h"])
        + tokens["cache_read"] * float(price["cache_read"])
        + tokens["output"] * float(price["output"])
    )
    return total / _MTOK


def _turn_cache_read_dollars(entry: dict[str, Any], tokens: dict[str, int], prompt: int) -> float:
    price = _price_for(entry, prompt)
    return tokens["cache_read"] * float(price["cache_read"]) / _MTOK


def _switch_premium(entry: dict[str, Any], tokens: dict[str, int], prompt: int) -> float:
    """Cold-switch premium: cache writes at write price minus the same tokens at read price."""
    price = _price_for(entry, prompt)
    written = tokens["cache_creation_5m"] + tokens["cache_creation_1h"]
    write_cost = tokens["cache_creation_5m"] * float(price["cache_write_5m"]) + tokens[
        "cache_creation_1h"
    ] * float(price["cache_write_1h"])
    read_cost = written * float(price["cache_read"])
    return (write_cost - read_cost) / _MTOK


def _tier(fam: str) -> int:
    if fam.startswith("fable"):
        return 4
    if fam.startswith("opus"):
        return 3
    if fam.startswith("sonnet"):
        return 2
    if fam.startswith("haiku"):
        return 1
    return 0


def _latest_family(families: dict[str, Any], prefix: str) -> str | None:
    matches = [f for f in families if f.startswith(prefix)]
    return max(matches) if matches else None


def _empty_tokens() -> dict[str, int]:
    return {k: 0 for k in _TOKEN_KINDS}


def _add_tokens(dst: dict[str, int], src: dict[str, int]) -> None:
    for k in _TOKEN_KINDS:
        dst[k] += src[k]


def _scan_turns(path: Path, families: dict[str, Any]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for obj in _iter_objects(path):
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        model = msg.get("model")
        if not isinstance(model, str) or not model.strip() or model == _SYNTHETIC_MODEL:
            continue
        tokens = _tokens(usage)
        turns.append(
            {
                "model": model,
                "family": _match_family(_normalize(model), families),
                "tokens": tokens,
                "weighted": _weighted(usage),
                "prompt": _prompt(tokens),
            }
        )
    return turns


def _sum_tokens(tokens: dict[str, int]) -> int:
    return sum(tokens.values())


def _model_entries(models: dict[str, dict[str, Any]], role_dollars: float) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for fam, m in models.items():
        entries.append(
            {
                "model": fam,
                "sessions": m["sessions"],
                "turns": m["turns"],
                "tokens": m["tokens"],
                "weighted_tokens": round(m["weighted"], 1),
                "dollars": round(m["dollars"], 4),
                "share_of_dollars": round(m["dollars"] / role_dollars, 4) if role_dollars else 0.0,
            }
        )
    entries.sort(key=lambda e: (-e["dollars"], e["model"]))
    return entries


def routing(
    root: Path,
    since: str = "30d",
    prices_path: Path | None = None,
    whatif: str = "both",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate per-model, per-role tokens and API-equivalent dollars."""
    if now is None:
        now = datetime.now(UTC)
    delta = _SINCE_DELTAS.get(since)
    cutoff = now - delta if delta is not None else None
    parents, subs = _discover(root, cutoff)

    prices = _load_prices(prices_path if prices_path is not None else _DEFAULT_PRICES)
    families: dict[str, Any] = prices[_FAMILIES_KEY]

    role_sessions = {"parent": len(parents), "subagent": len(subs)}
    role_turns: dict[str, int] = {r: 0 for r in _ROLES}
    role_tokens: dict[str, dict[str, int]] = {r: _empty_tokens() for r in _ROLES}
    role_weighted: dict[str, float] = {r: 0.0 for r in _ROLES}
    role_dollars: dict[str, float] = {r: 0.0 for r in _ROLES}
    role_cr_dollars: dict[str, float] = {r: 0.0 for r in _ROLES}
    role_models: dict[str, dict[str, dict[str, Any]]] = {r: {} for r in _ROLES}

    unpriced_tokens = _empty_tokens()
    unpriced_weighted = 0.0
    unpriced_turns = 0
    unpriced_sessions: set[str] = set()
    unpriced_models: dict[str, dict[str, Any]] = {}

    targets: dict[str, str | None] = {}
    if whatif in ("haiku", "both"):
        targets["haiku"] = _latest_family(families, "haiku")
    if whatif in ("sonnet", "both"):
        targets["sonnet"] = _latest_family(families, "sonnet")
    wa: dict[str, dict[str, Any]] = {}
    for name, fam in targets.items():
        wa[name] = {"family": fam, "actual": 0.0, "target": 0.0, "turns": 0, "sessions": set()}

    switch_cold = 0
    switch_warm = 0
    switch_unpriced = 0
    switch_sessions: set[str] = set()
    switch_ctx: list[int] = []
    switch_premiums: list[float] = []
    switch_transitions: dict[tuple[str, str], int] = {}

    for role, paths in (("parent", parents), ("subagent", [p for p, _ in subs])):
        for path in paths:
            turns = _scan_turns(path, families)
            seen_families: set[str] = set()
            seen_unpriced: set[str] = set()
            have_prev = False
            prev_fam: str | None = None
            for turn in turns:
                role_turns[role] += 1
                _add_tokens(role_tokens[role], turn["tokens"])
                role_weighted[role] += turn["weighted"]
                fam = turn["family"]
                if role == "parent" and have_prev and fam != prev_fam:
                    ctx = turn["prompt"]
                    cc = turn["tokens"]["cache_creation_5m"] + turn["tokens"]["cache_creation_1h"]
                    if fam is None or prev_fam is None:
                        switch_unpriced += 1
                    elif ctx > _COLD_CONTEXT and cc * 2 >= ctx:
                        switch_cold += 1
                        switch_sessions.add(str(path))
                        switch_ctx.append(ctx)
                        switch_premiums.append(_switch_premium(families[fam], turn["tokens"], ctx))
                    else:
                        switch_warm += 1
                    if fam is not None and prev_fam is not None:
                        key = (prev_fam, fam)
                        switch_transitions[key] = switch_transitions.get(key, 0) + 1
                if role == "parent":
                    have_prev = True
                    prev_fam = fam
                if fam is None:
                    unpriced_turns += 1
                    unpriced_weighted += turn["weighted"]
                    _add_tokens(unpriced_tokens, turn["tokens"])
                    unpriced_sessions.add(str(path))
                    slot = unpriced_models.setdefault(
                        turn["model"],
                        {
                            "turns": 0,
                            "tokens": _empty_tokens(),
                            "weighted": 0.0,
                            "sessions": 0,
                        },
                    )
                    slot["turns"] += 1
                    slot["weighted"] += turn["weighted"]
                    _add_tokens(slot["tokens"], turn["tokens"])
                    seen_unpriced.add(turn["model"])
                    continue

                entry = families[fam]
                dollars = _turn_dollars(entry, turn["tokens"], turn["prompt"])
                role_dollars[role] += dollars
                role_cr_dollars[role] += _turn_cache_read_dollars(
                    entry, turn["tokens"], turn["prompt"]
                )
                model = role_models[role].setdefault(
                    fam,
                    {
                        "sessions": 0,
                        "turns": 0,
                        "tokens": _empty_tokens(),
                        "weighted": 0.0,
                        "dollars": 0.0,
                    },
                )
                model["turns"] += 1
                model["weighted"] += turn["weighted"]
                model["dollars"] += dollars
                _add_tokens(model["tokens"], turn["tokens"])
                seen_families.add(fam)

                if role == "subagent":
                    for acc in wa.values():
                        target_fam = acc["family"]
                        if target_fam is not None and _tier(fam) > _tier(target_fam):
                            acc["actual"] += dollars
                            acc["target"] += _turn_dollars(
                                families[target_fam], turn["tokens"], turn["prompt"]
                            )
                            acc["turns"] += 1
                            acc["sessions"].add(str(path))

            for fam in seen_families:
                role_models[role][fam]["sessions"] += 1
            for m in seen_unpriced:
                unpriced_models[m]["sessions"] += 1

    total_dollars = role_dollars["parent"] + role_dollars["subagent"]
    total_weighted = role_weighted["parent"] + role_weighted["subagent"]
    total_tokens = _empty_tokens()
    _add_tokens(total_tokens, role_tokens["parent"])
    _add_tokens(total_tokens, role_tokens["subagent"])

    roles: dict[str, Any] = {}
    for role in _ROLES:
        tokens = role_tokens[role]
        token_sum = _sum_tokens(tokens)
        roles[role] = {
            "sessions": role_sessions[role],
            "turns": role_turns[role],
            "tokens": tokens,
            "weighted_tokens": round(role_weighted[role], 1),
            "dollars": round(role_dollars[role], 4),
            "share_of_dollars": round(role_dollars[role] / total_dollars, 4)
            if total_dollars
            else 0.0,
            "share_of_weighted_tokens": round(role_weighted[role] / total_weighted, 4)
            if total_weighted
            else 0.0,
            "cache_read_share_tokens": round(tokens["cache_read"] / token_sum, 4)
            if token_sum
            else 0.0,
            "cache_read_share_dollars": round(role_cr_dollars[role] / role_dollars[role], 4)
            if role_dollars[role]
            else 0.0,
            "models": _model_entries(role_models[role], role_dollars[role]),
        }

    unpriced_report: dict[str, Any] = {
        "tokens": unpriced_tokens,
        "weighted_tokens": round(unpriced_weighted, 1),
        "turns": unpriced_turns,
        "sessions": len(unpriced_sessions),
        "models": sorted(
            (
                {
                    "model": m,
                    "turns": slot["turns"],
                    "sessions": slot["sessions"],
                    "tokens": slot["tokens"],
                    "weighted_tokens": round(slot["weighted"], 1),
                }
                for m, slot in unpriced_models.items()
            ),
            key=lambda e: (-e["weighted_tokens"], e["model"]),
        ),
    }

    whatif_report: dict[str, Any] = {}
    for name in ("haiku", "sonnet"):
        scenario = wa.get(name)
        if scenario is None or scenario["family"] is None:
            whatif_report[name] = None
            continue
        change = scenario["target"] - scenario["actual"]
        whatif_report[name] = {
            "target": scenario["family"],
            "dollars_before": round(scenario["actual"], 4),
            "dollars_after": round(scenario["target"], 4),
            "change": round(change, 4),
            "percent_of_total": round(change / total_dollars * 100.0, 2) if total_dollars else 0.0,
            "turns": scenario["turns"],
            "sessions": len(scenario["sessions"]),
        }

    premium_total = sum(switch_premiums)
    model_switches: dict[str, Any] = {
        "cold": switch_cold,
        "warm": switch_warm,
        "sessions": len(switch_sessions),
        "unpriced": switch_unpriced,
        "context_tokens": {
            "median": round(percentile([float(v) for v in switch_ctx], 50), 1),
            "p90": round(percentile([float(v) for v in switch_ctx], 90), 1),
            "max": max(switch_ctx) if switch_ctx else 0,
        },
        "premium_total": round(premium_total, 4),
        "premium_median": round(percentile(switch_premiums, 50), 4),
        "premium_share_of_parent_dollars": round(premium_total / role_dollars["parent"], 4)
        if role_dollars["parent"]
        else 0.0,
        "transitions": [
            {"from": frm, "to": to, "count": count}
            for (frm, to), count in sorted(
                switch_transitions.items(),
                key=lambda kv: (-kv[1], kv[0][0], kv[0][1]),
            )[:5]
        ],
    }

    return {
        "since": since,
        "as_of": prices.get("as_of"),
        "source": prices.get("source"),
        "note": prices.get("note"),
        "disclaimer": _DISCLAIMER,
        "totals": {
            "sessions": role_sessions["parent"] + role_sessions["subagent"],
            "turns": role_turns["parent"] + role_turns["subagent"],
            "tokens": total_tokens,
            "weighted_tokens": round(total_weighted, 1),
            "dollars": round(total_dollars, 4),
        },
        "roles": roles,
        "unpriced": unpriced_report,
        "whatif": whatif_report,
        "model_switches": model_switches,
        "limits": list(_LIMITS),
    }


def _fmt_tokens(tokens: dict[str, int]) -> str:
    return (
        f"input {tokens['input']:,}, cache write 5m {tokens['cache_creation_5m']:,}, "
        f"cache write 1h {tokens['cache_creation_1h']:,}, cache read {tokens['cache_read']:,}, "
        f"output {tokens['output']:,}"
    )


def format_human(report: dict[str, Any], dir_name: str = ".") -> str:
    """Render the routing report as plain text (model ids, counts, tokens, dollars)."""
    if report["totals"]["sessions"] == 0:
        return f"No transcripts found under {dir_name}."
    label = _SINCE_LABELS.get(str(report["since"]), str(report["since"]))
    t = report["totals"]
    meta = []
    if report.get("as_of"):
        meta.append(f"as of {report['as_of']}")
    if report.get("source"):
        meta.append(f"source {report['source']}")
    if report.get("note"):
        meta.append(str(report["note"]))
    lines = [f"Claude Code routing, {label}", report["disclaimer"]]
    if meta:
        lines.append("; ".join(meta))
    lines.append(
        f"Totals: {t['sessions']:,} sessions, {t['turns']:,} turns, "
        f"{t['weighted_tokens']:,.1f} weighted tokens, ${t['dollars']:,.2f}"
    )
    lines.append("  " + _fmt_tokens(t["tokens"]))
    for role in _ROLES:
        r = report["roles"][role]
        lines.append("")
        lines.append(
            f"{role.capitalize()} ({r['sessions']:,} sessions, {r['turns']:,} turns, "
            f"${r['dollars']:,.2f}, {r['share_of_dollars'] * 100:.1f}% of dollars):"
        )
        lines.append(
            f"  cache read: {r['cache_read_share_tokens'] * 100:.1f}% of tokens, "
            f"{r['cache_read_share_dollars'] * 100:.1f}% of dollars"
        )
        for m in r["models"]:
            lines.append(
                f"  {m['model']}: {m['turns']:,} turns in {m['sessions']:,} sessions, "
                f"{m['weighted_tokens']:,.1f} weighted tokens, ${m['dollars']:,.2f} "
                f"({m['share_of_dollars'] * 100:.1f}%)"
            )
    u = report["unpriced"]
    lines.append("")
    lines.append(
        f"Unpriced models ({u['turns']:,} turns in {u['sessions']:,} sessions, "
        f"{u['weighted_tokens']:,.1f} weighted tokens, no dollars):"
    )
    if u["models"]:
        for m in u["models"]:
            lines.append(
                f"  {m['model']}: {m['turns']:,} turns, {m['weighted_tokens']:,.1f} weighted tokens"
            )
    else:
        lines.append("  (none)")
    ms = report["model_switches"]
    lines.append("")
    lines.append(
        f"Model switches: {ms['cold']:,} cold, {ms['warm']:,} warm, "
        f"{ms['unpriced']:,} involving an unpriced model"
    )
    if ms["cold"] == 0:
        lines.append("  no cold model switches in this window")
    else:
        ctx = ms["context_tokens"]
        lines.append(f"  cold switches in {ms['sessions']:,} parent sessions")
        lines.append(
            f"  context tokens per cold switch: median {ctx['median']:,.0f}, "
            f"p90 {ctx['p90']:,.0f}, max {ctx['max']:,}"
        )
        lines.append(
            f"  premium: total ${ms['premium_total']:,.4f}, "
            f"median ${ms['premium_median']:,.4f}, "
            f"{ms['premium_share_of_parent_dollars'] * 100:.2f}% of parent dollars"
        )
        if ms["transitions"]:
            transitions = ", ".join(
                f"{t['from']} -> {t['to']} ({t['count']})" for t in ms["transitions"]
            )
            lines.append(f"  top transitions: {transitions}")
    lines.append("")
    lines.append("What-if (sub-agent turns re-priced on a cheaper tier, same tokens):")
    for name in ("haiku", "sonnet"):
        w = report["whatif"].get(name)
        if w is None:
            continue
        lines.append(
            f"  {name}: ${w['dollars_before']:,.2f} -> ${w['dollars_after']:,.2f} "
            f"(change {w['change']:+,.2f}, {w['percent_of_total']:+.2f}% of total cost), "
            f"touches {w['turns']:,} turns in {w['sessions']:,} sessions"
        )
    lines.append("Limits:")
    for lim in report["limits"]:
        lines.append(f"  - {lim}")
    lines.append(
        "This report contains model ids, counts, tokens and dollars only; "
        "no paths, commands or text."
    )
    return "\n".join(lines)
