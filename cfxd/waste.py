"""``cfxd waste``: how much of a session's cost goes to loops, retries and rediscovery.

Counts and shares only — never paths, command arguments, prompts, tool output or
input hashes. Reuses the same weighted-token model as ``cfxd audit``/``replay``
(``input + 1.25*cache_creation + 0.1*cache_read + 5*output`` per assistant turn)
and attributes each turn's cost to the tool calls it issued, split evenly.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cfxd.audit import (
    _SINCE_DELTAS,
    _SINCE_LABELS,
    _discover,
    _iter_objects,
    _weighted,
)
from cfxd.daemon import percentile
from cfxd.usage_dedupe import MessageUsage

# A loop: the same signature 3+ times within a window of 10 consecutive tool calls.
_LOOP_WINDOW = 10
_LOOP_MIN = 3
# A retry streak: 3+ consecutive error results for the same tool name.
_RETRY_MIN = 3
# "Long" sessions (for first-third vs last-third cost accrual).
_LONG_TURNS = 60
# "Early" tool-call horizon for rediscovery.
_FIRST_N = 5

# Discovery verbs, in output order only via allow-list membership. Bash first
# word, plus the two-word ``git <sub>`` actions; Glob/Grep are keyed by tool name.
_DISCOVERY_VERBS = frozenset(
    {
        "ls",
        "pwd",
        "cat",
        "head",
        "tail",
        "find",
        "tree",
        "git status",
        "git log",
        "git branch",
        "which",
        "env",
        "echo",
    }
)


@dataclass
class _Call:
    name: str
    signature: str
    cost: float
    tool_use_id: str
    verb: str | None


@dataclass
class _Result:
    name: str
    is_error: bool
    cost: float
    tool_use_id: str


@dataclass
class _Session:
    turns: list[float]
    calls: list[_Call]
    results: list[_Result]


def _signature(name: str, inp: Any) -> str:
    """In-memory-only ``name`` + SHA-256 of the canonical JSON input."""
    canonical = json.dumps(inp, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{name}\x00{digest}"


def _is_error(block: dict[str, Any]) -> bool:
    """True when a tool result is flagged as an error (by key/shape, never text)."""
    if block.get("is_error") is True:
        return True
    content = block.get("content")
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "error" for b in content)
    return False


def _bash_verb(inp: Any) -> str | None:
    if not isinstance(inp, dict):
        return None
    command = inp.get("command")
    if not isinstance(command, str):
        return None
    words = command.split()
    if not words:
        return None
    verb = words[0]
    if verb == "git" and len(words) > 1:
        verb = "git " + words[1]
    return verb if verb in _DISCOVERY_VERBS else None


def _no_path_restriction(inp: Any) -> bool:
    if not isinstance(inp, dict):
        return False
    p = inp.get("path")
    return p is None or p == ""


def _verb(name: str, inp: Any) -> str | None:
    if name == "Bash":
        return _bash_verb(inp)
    if name in ("Glob", "Grep") and _no_path_restriction(inp):
        return name
    return None


def _scan(path: Path) -> _Session:
    """Stream one transcript into ordered turns, tool calls and tool results."""
    turns: list[float] = []
    calls: list[_Call] = []
    results: list[_Result] = []
    id_info: dict[str, tuple[str, float]] = {}

    dedupe = MessageUsage(_iter_objects(path))
    for obj in _iter_objects(path):
        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []

        usage = msg.get("usage") if isinstance(msg, dict) else None
        usage = usage if isinstance(usage, dict) else None

        if obj.get("type") == "assistant" and usage is not None:
            # Usage repeats on every line of one message.id: count the turn once, and
            # split the message's cost across all of its tool_use blocks.
            weighted = _weighted(dedupe.final_usage(msg, usage))
            if dedupe.count_here(msg):
                turns.append(weighted)
            uses = [b for b in blocks if b.get("type") == "tool_use"]
            n_uses = dedupe.tool_uses(msg) or len(uses)
            per_call = weighted / n_uses if uses else 0.0
            for b in uses:
                name = b.get("name")
                bid = b.get("id")
                if not isinstance(name, str) or not isinstance(bid, str):
                    continue
                inp = b.get("input")
                calls.append(_Call(name, _signature(name, inp), per_call, bid, _verb(name, inp)))
                id_info[bid] = (name, per_call)

        for b in blocks:
            if b.get("type") != "tool_result":
                continue
            tid = b.get("tool_use_id")
            if not isinstance(tid, str) or tid not in id_info:
                continue
            name, cost = id_info[tid]
            results.append(_Result(name, _is_error(b), cost, tid))

    return _Session(turns, calls, results)


def _loop_metrics(calls: list[_Call]) -> tuple[int, int, float, set[str]]:
    """Return (loops found, tool calls inside loops, loop cost, loop tool_use ids)."""
    sigs = [c.signature for c in calls]
    n = len(sigs)
    inside = [False] * n
    for i in range(n):
        lo = max(0, i - (_LOOP_WINDOW - 1))
        for sig, cnt in Counter(sigs[lo : i + 1]).items():
            if cnt >= _LOOP_MIN:
                for j in range(lo, i + 1):
                    if sigs[j] == sig:
                        inside[j] = True
    ids = {calls[i].tool_use_id for i in range(n) if inside[i]}
    cost = sum(calls[i].cost for i in range(n) if inside[i])
    found = len({calls[i].signature for i in range(n) if inside[i]})
    return found, sum(inside), cost, ids


def _retry_metrics(results: list[_Result]) -> tuple[list[int], float, set[str]]:
    """Return (streak lengths >= 3, streak cost, streak tool_use ids)."""
    streaks: list[int] = []
    cost = 0.0
    ids: set[str] = set()
    i = 0
    n = len(results)
    while i < n:
        name = results[i].name
        if not results[i].is_error:
            i += 1
            continue
        j = i
        while j < n and results[j].is_error and results[j].name == name:
            j += 1
        length = j - i
        if length >= _RETRY_MIN:
            streaks.append(length)
            for k in range(i, j):
                cost += results[k].cost
                ids.add(results[k].tool_use_id)
        i = j
    return streaks, cost, ids


def _pctl(values: list[int], p: float) -> float:
    return percentile([float(v) for v in values], p)


def _top_share(sorted_costs: list[float], total: float, fraction: float) -> float:
    if total <= 0 or not sorted_costs:
        return 0.0
    k = max(1, math.ceil(len(sorted_costs) * fraction - 1e-9))
    return sum(sorted_costs[:k]) / total


def waste(root: Path, since: str = "30d", now: datetime | None = None) -> dict[str, Any]:
    """Aggregate loop/retry/rediscovery waste over every transcript under ``root``."""
    if now is None:
        now = datetime.now(UTC)
    delta = _SINCE_DELTAS.get(since)
    cutoff = now - delta if delta is not None else None
    parents, subs = _discover(root, cutoff)
    paths = parents + [p for p, _ in subs]

    total_weighted = 0.0
    turn_counts: list[int] = []
    session_costs: list[float] = []

    loops_found = 0
    sessions_with_loops = 0
    loop_calls = 0
    loop_cost = 0.0

    streak_lengths: list[int] = []
    retry_cost = 0.0
    union_cost = 0.0

    verb_calls: Counter[str] = Counter()
    verb_first5: Counter[str] = Counter()
    total_tool_calls = 0

    long_count = 0
    long_first_sum = 0.0
    long_first_turns = 0
    long_last_sum = 0.0
    long_last_turns = 0

    for path in paths:
        s = _scan(path)
        if not s.turns:
            continue
        weighted = sum(s.turns)
        total_weighted += weighted
        session_costs.append(weighted)
        turn_counts.append(len(s.turns))

        found, calls_in, lcost, loop_ids = _loop_metrics(s.calls)
        loops_found += found
        loop_calls += calls_in
        loop_cost += lcost
        if calls_in > 0:
            sessions_with_loops += 1

        streaks, rcost, retry_ids = _retry_metrics(s.results)
        streak_lengths.extend(streaks)
        retry_cost += rcost
        union_cost += sum(c.cost for c in s.calls if c.tool_use_id in (loop_ids | retry_ids))

        for idx, c in enumerate(s.calls):
            total_tool_calls += 1
            if c.verb is not None:
                verb_calls[c.verb] += 1
                if idx < _FIRST_N:
                    verb_first5[c.verb] += 1

        if len(s.turns) >= _LONG_TURNS:
            long_count += 1
            third = len(s.turns) // 3
            first = s.turns[:third]
            last = s.turns[-third:]
            long_first_sum += sum(first)
            long_first_turns += len(first)
            long_last_sum += sum(last)
            long_last_turns += len(last)

    sorted_costs = sorted(session_costs, reverse=True)
    top10 = _top_share(sorted_costs, total_weighted, 0.10)
    top20 = _top_share(sorted_costs, total_weighted, 0.20)

    redis: dict[str, dict[str, Any]] = {}
    for verb in sorted(verb_calls, key=lambda v: -verb_calls[v]):
        count = verb_calls[verb]
        redis[verb] = {
            "calls": count,
            "share": round(count / total_tool_calls, 4) if total_tool_calls else 0.0,
            "first5_share": round(verb_first5[verb] / count, 4) if count else 0.0,
        }
    discovery_total = sum(verb_calls.values())

    loop_share = loop_cost / total_weighted if total_weighted else 0.0
    retry_share = retry_cost / total_weighted if total_weighted else 0.0
    union_share = union_cost / total_weighted if total_weighted else 0.0
    redis_share = discovery_total / total_tool_calls if total_tool_calls else 0.0

    return {
        "since": since,
        "sessions": len(session_costs),
        "total_weighted": round(total_weighted, 1),
        "loops": {
            "found": loops_found,
            "sessions_with_loops": sessions_with_loops,
            "tool_calls_in_loops": loop_calls,
            "cost_share": round(loop_share, 4),
        },
        "retries": {
            "streaks": len(streak_lengths),
            "longest": {
                "median": round(_pctl(streak_lengths, 50), 1),
                "p90": round(_pctl(streak_lengths, 90), 1),
                "max": max(streak_lengths) if streak_lengths else 0,
            },
            "cost_share": round(retry_share, 4),
        },
        "tail": {
            "top_10pct_cost_share": round(top10, 4),
            "top_20pct_cost_share": round(top20, 4),
            "turns_per_session": {
                "p50": round(_pctl(turn_counts, 50), 1),
                "p90": round(_pctl(turn_counts, 90), 1),
                "p99": round(_pctl(turn_counts, 99), 1),
            },
            "long_sessions": long_count,
            "cost_per_turn_first_third": round(long_first_sum / long_first_turns, 1)
            if long_first_turns
            else 0.0,
            "cost_per_turn_last_third": round(long_last_sum / long_last_turns, 1)
            if long_last_turns
            else 0.0,
        },
        "rediscovery": {
            "verbs": redis,
            "total_calls": discovery_total,
            "share_of_all_calls": round(redis_share, 4),
        },
        "summary": {
            "top_10pct": round(top10, 4),
            "loops_retries": round(union_share, 4),
            "rediscovery": round(redis_share, 4),
        },
    }


def format_waste(report: dict[str, Any], dir_name: str = ".") -> str:
    """Render the waste report as short plain text (counts and numbers only)."""
    if report["sessions"] == 0:
        return f"No transcripts found under {dir_name}."
    label = _SINCE_LABELS.get(str(report["since"]), str(report["since"]))
    lines = [
        f"Claude Code waste, {label}",
        f"Sessions: {report['sessions']:,}",
    ]
    lp = report["loops"]
    lines.append(
        f"Repeat loops: {lp['found']:,} loops in {lp['sessions_with_loops']:,} sessions, "
        f"{lp['tool_calls_in_loops']:,} tool calls inside loops "
        f"({lp['cost_share'] * 100:.1f}% of weighted cost)"
    )
    rt = report["retries"]
    lines.append(
        f"Retry streaks: {rt['streaks']:,} streaks, longest median "
        f"{rt['longest']['median']:.0f}, p90 {rt['longest']['p90']:.0f}, "
        f"max {rt['longest']['max']} ({rt['cost_share'] * 100:.1f}% of weighted cost)"
    )
    tl = report["tail"]
    lines.append(
        f"Tail: top 10% of sessions {tl['top_10pct_cost_share'] * 100:.1f}% of weighted "
        f"cost, top 20% {tl['top_20pct_cost_share'] * 100:.1f}%; turns/session "
        f"p50 {tl['turns_per_session']['p50']:.0f}, p90 {tl['turns_per_session']['p90']:.0f}, "
        f"p99 {tl['turns_per_session']['p99']:.0f}"
    )
    lines.append(
        f"  long sessions (>={_LONG_TURNS} turns): {tl['long_sessions']}, cost/turn first "
        f"third {tl['cost_per_turn_first_third']:.0f} vs last third "
        f"{tl['cost_per_turn_last_third']:.0f}"
    )
    lines.append("Rediscovery verbs:")
    for verb, st in report["rediscovery"]["verbs"].items():
        lines.append(
            f"  {verb}: {st['calls']:,} calls ({st['share'] * 100:.1f}% of all tool calls, "
            f"{st['first5_share'] * 100:.1f}% in first 5)"
        )
    s = report["summary"]
    lines.append("Summary:")
    lines.append(
        f"  {s['top_10pct'] * 100:.1f}% of weighted cost sits in the most expensive 10% of "
        f"sessions (exploratory, one machine)"
    )
    lines.append(
        f"  loops and retry streaks account for {s['loops_retries'] * 100:.1f}% "
        f"(exploratory, one machine)"
    )
    lines.append(
        f"  rediscovery verbs account for {s['rediscovery'] * 100:.1f}% of tool calls "
        f"(exploratory, one machine)"
    )
    lines.append("This report contains counts and numbers only; no paths, commands or text.")
    return "\n".join(lines)
