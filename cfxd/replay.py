"""``cfxd replay``: offline upper-bound estimate of shrinking old tool results.

Counts and numbers only — never paths, commands, prompts or transcript text
(same rule and canary test as ``cfxd audit``).

Model (see strategy memo D4 and the cache-invalidation paragraph in C):

- Per transcript file, parent and sub-agent files are simulated separately. We
  reconstruct the ordered assistant turns (usage + timestamp) and, for each
  tool_result, its byte size and the assistant-turn index at which it entered
  context (the first assistant turn after the result appears).
- Tool-result tokens are estimated as ``bytes / 4``.
- Baseline turn cost = ``input + 1.25*cache_creation + 0.1*cache_read +
  5*output`` from the recorded usage (same weighting as ``cfxd audit``).
- Policy S(k, batch): at a turn t (a shrink event iff ``t % batch == 0``) every
  tool_result of at least ``min-bytes`` that entered more than ``k`` turns ago
  (``t - entered > k``) and is not yet shrunk is replaced by a ``stub-bytes``
  stub. The removed tokens ``X = (size - stub) / 4`` leave the context, so every
  later turn's cache_read falls by ``min(X, cache_read)``, priced at 0.1.
- Re-creation penalty (conservative variant only): at a shrink turn the context
  tail after the earliest edited result is re-created at 1.25 instead of read at
  0.1, an extra ``1.15 * tail`` where ``tail = max(0, cache_read +
  cache_creation - tool-result tokens before the earliest edited result)``. Only
  tool-result sizes are known (prompt/assistant/system text is not counted), so
  "before" is an underestimate and the penalty an overestimate — conservative.
  We also price the whole tail as previously-read (0.1), overstating the penalty
  whenever part of it was already cache_creation. The optimistic variant drops
  this penalty entirely.
- Re-fetch sensitivity ``r`` in {0, 0.1, 0.3}: one turn after a result is shrunk,
  a fraction ``r`` of its full bytes is re-read, adding ``1.25 * r * (size/4)``
  cache creation once and restoring ``r * (size/4)`` tokens to context from then
  on (expected-value scaling; deterministic).
- Compaction boundaries reset the shrink state: at the next assistant turn the
  shrunk set and removed-token accounting are cleared, and tool results that
  entered before the boundary are treated as out of context (summarised away).
- Cache-expiry: consecutive assistant turns with timestamps in a file are
  classified by the gap since the previous assistant turn (<=5m, 5-60m, >60m).
  A turn with no previous timestamp is excluded from the classes but its cost
  still counts toward the "total" denominators. A cold-cache turn has
  ``cache_read < 0.1 * (cache_read + cache_creation)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cfxd.audit import (
    _SINCE_DELTAS,
    _as_int,
    _discover,
    _iter_objects,
    _texts,
    _weighted,
)

R_FRACTIONS = (0.0, 0.1, 0.3)
VARIANTS = ("conservative", "optimistic")

_GAP_LE5 = "<=5m"
_GAP_5TO60 = "5-60m"
_GAP_GT60 = ">60m"


@dataclass
class _Turn:
    weighted: float
    cr: int
    cc: int
    ts: datetime | None


@dataclass
class _Result:
    size: int
    tokens: float
    entered_at: int = -1


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _reconstruct(path: Path) -> tuple[list[_Turn], list[_Result], list[bool]]:
    """Stream one transcript into turns, tool results, and reset markers.

    Returns ``(turns, results, resets)`` where ``resets[t]`` is True when a
    compaction boundary occurs immediately before turn ``t``.
    """
    turns: list[_Turn] = []
    results: list[_Result] = []
    pending: list[_Result] = []
    resets: list[bool] = []
    reset_next = False

    for obj in _iter_objects(path):
        if "isCompactSummary" in obj or obj.get("subtype") == "compact_boundary":
            reset_next = True
            continue

        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []

        ts = _parse_ts(obj.get("timestamp") if isinstance(obj, dict) else None)
        if ts is None and isinstance(msg, dict):
            ts = _parse_ts(msg.get("timestamp"))

        usage = msg.get("usage") if isinstance(msg, dict) else None
        usage = usage if isinstance(usage, dict) else None

        if obj.get("type") == "assistant" and usage is not None:
            for r in pending:
                r.entered_at = len(turns)
                results.append(r)
            pending = []
            resets.append(reset_next)
            reset_next = False
            turns.append(
                _Turn(
                    weighted=_weighted(usage),
                    cr=_as_int(usage.get("cache_read_input_tokens")) or 0,
                    cc=_as_int(usage.get("cache_creation_input_tokens")) or 0,
                    ts=ts,
                )
            )

        for block in blocks:
            if block.get("type") != "tool_result":
                continue
            texts = _texts(block.get("content"))
            nbytes = sum(len(t.encode("utf-8")) for t in texts)
            pending.append(_Result(size=nbytes, tokens=nbytes / 4.0))

    return turns, results, resets


def _simulate(
    turns: list[_Turn],
    results: list[_Result],
    resets: list[bool],
    k: int,
    batch: int,
    min_bytes: int,
    stub_bytes: int,
    r: float,
    conservative: bool,
) -> float:
    """Return the simulated weighted cost of one transcript under one policy."""
    stub_tokens = stub_bytes / 4.0
    n = len(turns)
    shrunk: set[int] = set()
    shrunk_at_turn: dict[int, list[int]] = {}
    active_removed = 0.0
    window_start = 0
    total = 0.0

    for t in range(n):
        if resets[t]:
            shrunk.clear()
            shrunk_at_turn.clear()
            active_removed = 0.0
            window_start = t

        turn = turns[t]
        w = turn.weighted

        prev = shrunk_at_turn.get(t - 1)
        if prev:
            re = r * sum(results[i].tokens for i in prev)
            active_removed = max(0.0, active_removed - re)
            w += 1.25 * re

        if t % batch == 0:
            newly = [
                i
                for i, res in enumerate(results)
                if res.entered_at >= window_start
                and t - res.entered_at > k
                and res.size >= min_bytes
                and i not in shrunk
            ]
            if newly:
                for i in newly:
                    shrunk.add(i)
                    active_removed += results[i].tokens - stub_tokens
                shrunk_at_turn[t] = newly
                if conservative:
                    earliest = min(results[i].entered_at for i in newly)
                    before = sum(
                        (stub_tokens if i in shrunk else results[i].tokens)
                        for i, res in enumerate(results)
                        if window_start <= res.entered_at < earliest
                    )
                    tail = max(0.0, float(turn.cr + turn.cc) - before)
                    w += 1.15 * tail

        w -= 0.1 * min(active_removed, float(turn.cr))
        total += w

    return total


def _parse_csv_ints(value: str, default: tuple[int, ...]) -> tuple[int, ...]:
    if value is None:
        return default
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        return default
    return tuple(int(p) for p in parts)


def _gap_class(gap: timedelta) -> str:
    if gap <= timedelta(minutes=5):
        return _GAP_LE5
    if gap <= timedelta(minutes=60):
        return _GAP_5TO60
    return _GAP_GT60


def replay(
    root: Path,
    since: str = "30d",
    ks: tuple[int, ...] = (3, 5, 10),
    batches: tuple[int, ...] = (1, 5),
    min_bytes: int = 2048,
    stub_bytes: int = 300,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate the replay simulation over every transcript under ``root``."""
    if now is None:
        now = datetime.now(UTC)
    delta = _SINCE_DELTAS.get(since)
    cutoff = now - delta if delta is not None else None
    parents, subs = _discover(root, cutoff)
    paths = parents + [p for p, _ in subs]

    sessions = 0
    sessions_over_30 = 0
    baseline_total = 0.0
    baseline_over_30 = 0.0

    sim_total: dict[tuple[int, int, str, float], list[float]] = {}
    for k in ks:
        for b in batches:
            for var in VARIANTS:
                for r in R_FRACTIONS:
                    sim_total[(k, b, var, r)] = [0.0, 0.0]

    cache_classes = {_GAP_LE5: 0.0, _GAP_5TO60: 0.0, _GAP_GT60: 0.0}
    cache_cc = {_GAP_LE5: 0.0, _GAP_5TO60: 0.0, _GAP_GT60: 0.0}
    cache_weighted = {_GAP_LE5: 0.0, _GAP_5TO60: 0.0, _GAP_GT60: 0.0}
    cache_cold_cc = {_GAP_LE5: 0.0, _GAP_5TO60: 0.0, _GAP_GT60: 0.0}
    cache_total_cc = 0.0
    cache_total_weighted = 0.0

    for path in paths:
        turns, results, resets = _reconstruct(path)
        if not turns:
            continue
        sessions += 1
        baseline = sum(t.weighted for t in turns)
        baseline_total += baseline
        over_30 = len(turns) > 30
        if over_30:
            sessions_over_30 += 1
            baseline_over_30 += baseline

        for (k, b, var, r), acc in sim_total.items():
            sim = _simulate(
                turns, results, resets, k, b, min_bytes, stub_bytes, r, var == "conservative"
            )
            acc[0] += sim
            if over_30:
                acc[1] += sim

        _accumulate_cache(turns, cache_classes, cache_cc, cache_weighted, cache_cold_cc)
        cache_total_cc += sum(t.cc for t in turns)
        cache_total_weighted += sum(t.weighted for t in turns)

    policies: list[dict[str, Any]] = []
    for k in ks:
        for b in batches:
            variants: list[dict[str, Any]] = []
            for var in VARIANTS:
                for r in R_FRACTIONS:
                    overall, over_30_sim = sim_total[(k, b, var, r)]
                    variants.append(
                        {
                            "variant": var,
                            "r": r,
                            "simulated_weighted": round(overall, 1),
                            "pct_saved": round(_pct(baseline_total, overall), 1),
                            "pct_saved_over_30": round(_pct(baseline_over_30, over_30_sim), 1),
                        }
                    )
            policies.append({"k": k, "batch": b, "variants": variants})

    cache_expiry: dict[str, dict[str, float]] = {}
    for cls in (_GAP_LE5, _GAP_5TO60, _GAP_GT60):
        cache_expiry[cls] = {
            "turns": cache_classes[cls],
            "cc_share": round(cache_cc[cls] / cache_total_cc, 4) if cache_total_cc else 0.0,
            "weighted_share": round(cache_weighted[cls] / cache_total_weighted, 4)
            if cache_total_weighted
            else 0.0,
            "cold_cc_share": round(cache_cold_cc[cls] / cache_total_cc, 4)
            if cache_total_cc
            else 0.0,
        }

    return {
        "since": since,
        "sessions": sessions,
        "sessions_over_30": sessions_over_30,
        "baseline_weighted": round(baseline_total, 1),
        "policies": policies,
        "cache_expiry": cache_expiry,
    }


def _accumulate_cache(
    turns: list[_Turn],
    classes: dict[str, float],
    cc: dict[str, float],
    weighted: dict[str, float],
    cold_cc: dict[str, float],
) -> None:
    prev_ts: datetime | None = None
    for turn in turns:
        if turn.ts is None:
            prev_ts = None
            continue
        if prev_ts is not None:
            cls = _gap_class(turn.ts - prev_ts)
            classes[cls] += 1.0
            cc[cls] += turn.cc
            weighted[cls] += turn.weighted
            if turn.cr < 0.1 * (turn.cr + turn.cc):
                cold_cc[cls] += turn.cc
        prev_ts = turn.ts


def _pct(baseline: float, simulated: float) -> float:
    if baseline <= 0:
        return 0.0
    return (baseline - simulated) / baseline * 100.0


def format_replay(report: dict[str, Any], dir_name: str = ".") -> str:
    """Render the replay report as short plain text (counts and numbers only)."""
    if report["sessions"] == 0:
        return f"No transcripts found under {dir_name}."
    lines = [
        f"Claude Code replay, last {report['since']}",
        (
            f"Sessions: {report['sessions']:,}   "
            f"sessions over 30 turns: {report['sessions_over_30']:,}"
        ),
        f"Baseline weighted cost: {report['baseline_weighted']:,.1f}",
    ]
    for policy in report["policies"]:
        lines.append(f"Policy k={policy['k']} batch={policy['batch']}:")
        for v in policy["variants"]:
            lines.append(
                f"  {v['variant']} r={v['r']}: sim {v['simulated_weighted']:,.1f}, "
                f"saved {v['pct_saved']:.1f}% overall, {v['pct_saved_over_30']:.1f}% over 30 turns"
            )
    lines.append("Cache expiry (gap since previous turn):")
    for cls in (_GAP_LE5, _GAP_5TO60, _GAP_GT60):
        e = report["cache_expiry"][cls]
        lines.append(
            f"  {cls}: {int(e['turns'])} turns, {e['cc_share'] * 100:.1f}% cc, "
            f"{e['weighted_share'] * 100:.1f}% weighted, {e['cold_cc_share'] * 100:.1f}% cold cc"
        )
    lines.append("This report contains counts and numbers only; no paths, commands or text.")
    return "\n".join(lines)
