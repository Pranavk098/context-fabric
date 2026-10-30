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

Reset policies R(k, s) (upper bound on compacting/restarting a long session):
- Context at turn t is ``C(t) = input + cache_creation + cache_read``; the
  starting prefix is ``P = C(1)`` of the current segment. At the first turn
  ``t0 >= k`` (and every k turns after) the context is replaced by ``P + s``
  (the prefix plus a ``s``-token summary). At ``t0`` the whole ``P + s`` is a
  cache miss (1.25 per token); on later turns the unchanged part is read at 0.1
  and the growth ``C(t) - C(t0)`` is priced at the recorded turn's average input
  rate (the recorded creation/read split, scaled). Output stays as recorded.
- ``with_summary_cost`` variant: one extra turn per reset, charging the full
  ``C(t0)`` as a cache read (0.1) plus ``s`` output tokens at 5x.
- Re-fetch fraction ``f``: at a reset the dropped context is ``D = C(t0) - P``;
  a fraction ``f`` of it re-enters context from ``t0+1`` onward (read at 0.1 per
  later turn, charged once as cache creation 1.25/token at ``t0+1``). At the next
  reset the re-fetched content is dropped like the rest, so ``D`` compounds:
  ``D_j = C(r_j) - C(r_{j-1}) + f*D_{j-1}``. ``f = 0`` reproduces the original
  numbers exactly.
- This is an UPPER BOUND: a perfect summary loses nothing, summary production is
  free in the main variant, the user's time is ignored, and cache expiry across
  idle gaps is not modelled. Recorded compaction boundaries already in the
  transcript restart the segment (treated as an existing reset, never counted
  twice). Sessions shorter than k are unchanged.
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
from cfxd.usage_dedupe import MessageUsage

R_FRACTIONS = (0.0, 0.1, 0.3)
VARIANTS = ("conservative", "optimistic")
RESET_FRACTIONS = (0.0, 0.1, 0.25, 0.5)
_F_GRID = tuple(i / 20.0 for i in range(21))  # 0.0, 0.05, ..., 1.0

_GAP_LE5 = "<=5m"
_GAP_5TO60 = "5-60m"
_GAP_GT60 = ">60m"


@dataclass
class _Turn:
    weighted: float
    inp: int
    cc: int
    cr: int
    out: int
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

    dedupe = MessageUsage(_iter_objects(path))
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

        if obj.get("type") == "assistant" and usage is not None and dedupe.count_here(msg):
            for r in pending:
                r.entered_at = len(turns)
                results.append(r)
            pending = []
            resets.append(reset_next)
            reset_next = False
            turns.append(
                _Turn(
                    weighted=_weighted(usage),
                    inp=_as_int(usage.get("input_tokens")) or 0,
                    cc=_as_int(usage.get("cache_creation_input_tokens")) or 0,
                    cr=_as_int(usage.get("cache_read_input_tokens")) or 0,
                    out=_as_int(usage.get("output_tokens")) or 0,
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


def _context(turn: _Turn) -> int:
    return turn.inp + turn.cc + turn.cr


def _simulate_reset(
    turns: list[_Turn],
    resets: list[bool],
    k: int,
    s: int,
    with_summary_cost: bool,
    f: float = 0.0,
) -> float:
    """Return the simulated weighted cost of one transcript under reset policy R(k, s).

    A reset turns the whole context into the starting prefix P (context at the first
    turn of the current segment) plus a ``s``-token summary. At the reset turn that
    P + s is a cache miss (1.25 per token); on later turns P + s is read at 0.1 and the
    growth since the reset is priced at the recorded turn's average input rate
    (``(inp + 1.25*cc + 0.1*cr) / C(t)``), an approximation of the recorded creation/read
    split. Recorded compaction boundaries restart the segment (an existing reset).

    A fraction ``f`` of the dropped context D = C(t0) - P is re-fetched: ``f*D`` tokens
    re-enter context from t0+1 onward, charged once as cache creation (1.25/token) at
    t0+1 and read at 0.1 on every later turn. The re-fetched content is dropped at the
    next reset and re-fetched again, so D compounds across resets. ``f = 0`` reproduces
    the original numbers exactly.
    """
    n = len(turns)
    if n < k:
        return sum(t.weighted for t in turns)
    total = 0.0
    seg_start = 0
    prefix = _context(turns[0])
    last_reset_ctx = float(prefix)
    refetch = 0.0
    for t in range(n):
        turn = turns[t]
        c_t = _context(turn)
        if resets[t]:
            seg_start = t
            prefix = c_t
            last_reset_ctx = float(c_t)
            refetch = 0.0
        since = t - seg_start
        if since < k - 1:
            total += turn.weighted
            continue
        if (since - (k - 1)) % k == 0:
            dropped = max(0.0, float(c_t) - last_reset_ctx) + refetch
            refetch = f * dropped
            last_reset_ctx = float(c_t)
            cost = 1.25 * (prefix + s) + 5.0 * turn.out
            if with_summary_cost:
                cost += 0.1 * c_t + 5.0 * s
            total += cost
            continue
        r = seg_start + (k - 1) + ((since - (k - 1)) // k) * k
        growth = max(0.0, float(c_t - _context(turns[r])))
        rate = (turn.weighted - 5.0 * turn.out) / c_t if c_t > 0 else 0.0
        total += 0.1 * (prefix + s + refetch) + growth * rate + 5.0 * turn.out
        if t == r + 1:
            total += 1.25 * refetch
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
    reset_ks: tuple[int, ...] = (40, 80, 120),
    summary_tokens: tuple[int, ...] = (2000, 5000),
    refetch: tuple[float, ...] = RESET_FRACTIONS,
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

    reset_tot: dict[tuple[int, int, float], dict[str, float]] = {}
    all_f = tuple(dict.fromkeys((*refetch, *_F_GRID)))
    for k in reset_ks:
        for s in summary_tokens:
            for f in all_f:
                reset_tot[(k, s, f)] = {
                    "sim_main": 0.0,
                    "sim_summary": 0.0,
                    "aff_sim_main": 0.0,
                    "aff_sim_summary": 0.0,
                }

    reset_aff: dict[tuple[int, int], dict[str, float]] = {}
    for k in reset_ks:
        for s in summary_tokens:
            reset_aff[(k, s)] = {"aff_baseline": 0.0, "aff_count": 0.0}

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

        for (k, s, f), racc in reset_tot.items():
            sim_main = _simulate_reset(turns, resets, k, s, False, f)
            sim_summary = _simulate_reset(turns, resets, k, s, True, f)
            racc["sim_main"] += sim_main
            racc["sim_summary"] += sim_summary
            if len(turns) >= k:
                racc["aff_sim_main"] += sim_main
                racc["aff_sim_summary"] += sim_summary

        for (k, _s), aff in reset_aff.items():
            if len(turns) >= k:
                aff["aff_count"] += 1.0
                aff["aff_baseline"] += baseline

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

    reset_policies: list[dict[str, Any]] = []
    break_even: list[dict[str, Any]] = []
    break_even_f: list[dict[str, Any]] = []
    for s in summary_tokens:
        be_k: int | None = None
        for k in reset_ks:
            if be_k is None and reset_tot[(k, s, 0.0)]["sim_main"] < baseline_total:
                be_k = k
        break_even.append({"summary_tokens": s, "break_even_k": be_k})

    for s in summary_tokens:
        for k in reset_ks:
            aff = reset_aff[(k, s)]
            aff_baseline = aff["aff_baseline"]
            rvariants: list[dict[str, Any]] = []
            for variant, main_key, aff_key in (
                ("upper_bound", "sim_main", "aff_sim_main"),
                ("with_summary_cost", "sim_summary", "aff_sim_summary"),
            ):
                for f in refetch:
                    racc = reset_tot[(k, s, f)]
                    rvariants.append(
                        {
                            "variant": variant,
                            "f": f,
                            "simulated_weighted": round(racc[main_key], 1),
                            "pct_saved": round(_pct(baseline_total, racc[main_key]), 1),
                            "pct_saved_affected": round(_pct(aff_baseline, racc[aff_key]), 1),
                        }
                    )
            reset_policies.append(
                {
                    "k": k,
                    "summary_tokens": s,
                    "sessions_simulated": sessions,
                    "sessions_affected": int(aff["aff_count"]),
                    "affected_cost_share": round(aff_baseline / baseline_total, 4)
                    if baseline_total
                    else 0.0,
                    "baseline_weighted": round(baseline_total, 1),
                    "variants": rvariants,
                }
            )
            ub_totals = [reset_tot[(k, s, f)]["sim_main"] for f in _F_GRID]
            wsc_totals = [reset_tot[(k, s, f)]["sim_summary"] for f in _F_GRID]
            break_even_f.append(
                {
                    "k": k,
                    "summary_tokens": s,
                    "upper_bound": {
                        "break_even_f": _break_even_f(baseline_total, ub_totals),
                        "f_below_10pct": _f_below(baseline_total, ub_totals, 10.0),
                    },
                    "with_summary_cost": {
                        "break_even_f": _break_even_f(baseline_total, wsc_totals),
                        "f_below_10pct": _f_below(baseline_total, wsc_totals, 10.0),
                    },
                }
            )

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
        "reset_policies": reset_policies,
        "reset_break_even": break_even,
        "reset_break_even_f": break_even_f,
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


def _fmt_f(f: float | None) -> str:
    return "none" if f is None else f"{f:.2f}"


def _break_even_f(baseline: float, totals: list[float]) -> float | None:
    """Largest grid f where the simulated total still beats baseline (saving > 0)."""
    best: float | None = None
    for f, total in zip(_F_GRID, totals, strict=True):
        if total < baseline:
            best = f
    return best


def _f_below(baseline: float, totals: list[float], pct: float) -> float | None:
    """Smallest grid f where saving falls below ``pct`` (sim > (1-pct/100)*baseline)."""
    floor = baseline * (1.0 - pct / 100.0)
    for f, total in zip(_F_GRID, totals, strict=True):
        if total > floor:
            return f
    return None


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
    lines.append(
        "Reset policies (UPPER BOUND: a perfect summary loses nothing, summary cost is "
        "charged only in the second variant, the user's time and cache expiry across idle "
        "gaps are ignored). f is the re-fetch fraction, an assumption to be measured "
        "(a paired benchmark can estimate it):"
    )
    be_f = {(b["k"], b["summary_tokens"]): b for b in report["reset_break_even_f"]}
    for rp in report["reset_policies"]:
        lines.append(f"R(k={rp['k']}, S={rp['summary_tokens']}):")
        lines.append(
            f"  sessions {rp['sessions_simulated']:,}, affected (>= {rp['k']} turns) "
            f"{rp['sessions_affected']:,} ({rp['affected_cost_share'] * 100:.1f}% of baseline cost)"
        )
        for v in rp["variants"]:
            lines.append(
                f"  {v['variant']} f={v['f']}: sim {v['simulated_weighted']:,.1f}, "
                f"saved {v['pct_saved']:.1f}% overall, {v['pct_saved_affected']:.1f}% "
                f"over affected sessions"
            )
        bf = be_f.get((rp["k"], rp["summary_tokens"]))
        if bf is not None:
            lines.append(
                f"  break-even f: upper_bound={_fmt_f(bf['upper_bound']['break_even_f'])}, "
                f"with_summary_cost={_fmt_f(bf['with_summary_cost']['break_even_f'])}"
            )
            if rp["k"] == 40 and rp["summary_tokens"] == 5000:
                lines.append(
                    f"  saving falls below 10% at f={_fmt_f(bf['upper_bound']['f_below_10pct'])} "
                    f"(upper_bound), {_fmt_f(bf['with_summary_cost']['f_below_10pct'])} "
                    f"(with_summary_cost)"
                )
    lines.append("Reset break-even (smallest k with positive saving):")
    for be in report["reset_break_even"]:
        be_k = be["break_even_k"]
        lines.append(f"  S={be['summary_tokens']}: {'none' if be_k is None else f'k={be_k}'}")
    lines.append("Cache expiry (gap since previous turn):")
    for cls in (_GAP_LE5, _GAP_5TO60, _GAP_GT60):
        e = report["cache_expiry"][cls]
        lines.append(
            f"  {cls}: {int(e['turns'])} turns, {e['cc_share'] * 100:.1f}% cc, "
            f"{e['weighted_share'] * 100:.1f}% weighted, {e['cold_cc_share'] * 100:.1f}% cold cc"
        )
    lines.append("This report contains counts and numbers only; no paths, commands or text.")
    return "\n".join(lines)
