"""Counts-and-sizes report over agent-neutral sessions.

Same privacy rule as ``cfxd audit``: no paths, commands, prompts, tool output
or arguments appear; tool names are kept only when they are plain identifiers.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from cfxd.agents.model import Session
from cfxd.daemon import percentile

_TEN_KB = 10 * 1024
_TWENTYFIVE_KB = 25 * 1024

_USAGE_KINDS = ("input_uncached", "cache_read", "cache_write", "output", "reasoning")


def _pctl(values: list[int], p: float) -> float:
    return percentile([float(v) for v in values], p)


def build_report(agent: str, sessions: Iterable[Session]) -> dict[str, Any]:
    """Aggregate sessions into a counts-and-sizes report dict."""
    sess_list = list(sessions)
    turn_counts = [len(s.turns) for s in sess_list]
    subagent_count = sum(1 for s in sess_list if s.is_subagent)

    usage: dict[str, int] = {k: 0 for k in _USAGE_KINDS}
    for s in sess_list:
        for turn in s.turns:
            u = turn.usage
            usage["input_uncached"] += u.input_uncached
            usage["cache_read"] += u.cache_read
            usage["cache_write"] += u.cache_write
            usage["output"] += u.output
            usage["reasoning"] += u.reasoning
    weighted = (
        usage["input_uncached"]
        + 1.25 * usage["cache_write"]
        + 0.1 * usage["cache_read"]
        + 5 * usage["output"]
    )

    tool_sizes: dict[str, list[int]] = {}
    for s in sess_list:
        for tr in s.tool_results:
            tool_sizes.setdefault(tr.tool, []).append(tr.bytes)

    tools: dict[str, dict[str, Any]] = {}
    total_bytes = sum(sum(sizes) for sizes in tool_sizes.values())
    share_over_10k: dict[str, float] = {}
    for name in sorted(tool_sizes, key=lambda n: -sum(tool_sizes[n])):
        sizes = tool_sizes[name]
        over_10 = [x for x in sizes if x > _TEN_KB]
        over_25 = [x for x in sizes if x > _TWENTYFIVE_KB]
        tools[name] = {
            "count": len(sizes),
            "bytes": sum(sizes),
            "p50": round(_pctl(sizes, 50), 1),
            "p90": round(_pctl(sizes, 90), 1),
            "max": max(sizes) if sizes else 0,
            "over_10k": len(over_10),
            "over_25k": len(over_25),
        }
        if total_bytes:
            share_over_10k[name] = round(sum(over_10) / total_bytes, 4)

    return {
        "agent": agent,
        "sessions": len(sess_list) - subagent_count,
        "subagent_sessions": subagent_count,
        "turns": {
            "total": sum(turn_counts),
            "median": round(_pctl(turn_counts, 50), 1),
            "p90": round(_pctl(turn_counts, 90), 1),
            "sessions_over_30": sum(1 for c in turn_counts if c > 30),
        },
        "usage": {**usage, "weighted_tokens": round(weighted, 1)},
        "tools": tools,
        "share_over_10k": share_over_10k,
        "compactions": {"count": sum(s.compactions for s in sess_list)},
    }


def format_human(report: dict[str, Any], dir_name: str = ".") -> str:
    """Render the report as short plain text (counts and sizes only)."""
    agent = report["agent"]
    if report["sessions"] == 0 and report["subagent_sessions"] == 0:
        return f"No {agent} sessions found under {dir_name} in this time window (try --since all)."
    lines = [
        f"{agent} sessions",
        f"Sessions: {report['sessions']:,}   sub-agent sessions: {report['subagent_sessions']:,}",
    ]
    t = report["turns"]
    lines.append(
        f"Turns: {t['total']:,} total, median {t['median']:.1f}, p90 {t['p90']:.1f}, "
        f"{t['sessions_over_30']} sessions over 30 turns"
    )
    u = report["usage"]
    lines.append("Token usage (raw totals):")
    lines.append(f"  input (uncached): {u['input_uncached']:,}")
    lines.append(f"  cache read: {u['cache_read']:,}")
    lines.append(f"  cache write: {u['cache_write']:,}")
    lines.append(f"  output: {u['output']:,}")
    lines.append(f"  reasoning: {u['reasoning']:,}")
    lines.append(f"  weighted (Anthropic-style weighting, indicative): {u['weighted_tokens']:,.1f}")
    lines.append("Tool-result bytes:")
    for name, st in report["tools"].items():
        lines.append(
            f"  {name}: {st['count']:,} results, {st['bytes']:,} bytes "
            f"(p50 {st['p50']:.0f}, p90 {st['p90']:.0f}, max {st['max']:,}, "
            f"{st['over_10k']} over 10 KB, {st['over_25k']} over 25 KB)"
        )
    shares = ", ".join(f"{k} {v * 100:.1f}%" for k, v in report["share_over_10k"].items())
    lines.append(f"Share of tool-result bytes over 10 KB: {shares}")
    lines.append(f"Compactions: {report['compactions']['count']}")
    lines.append("This report contains counts and sizes only; no paths, commands or text.")
    return "\n".join(lines)
