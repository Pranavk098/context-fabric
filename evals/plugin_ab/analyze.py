"""Paired analysis of plugin A/B run records.

Computes, from run records: paired per-task log cost ratios (on/off, mean of
non-warm-up reps), the geometric mean ratio with a bootstrap 95% CI, resolve
rate per arm, on/off disagreement counts, per-protocol vs intention-to-treat
cost, plugin overhead, and a losses table. States the pre-registered claim rule
and whether the data meets it; never states a saving the data does not support.
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any

CLAIM_CHEAPER = 0.15  # at least 15% cheaper
CLAIM_NON_INFERIOR_TASKS = 1  # on may fail to resolve at most 1 more task than off
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20261024


def _measured(records: list[dict[str, Any]], task: str, arm: str) -> list[dict[str, Any]]:
    return [
        r for r in records if r.get("task") == task and r.get("arm") == arm and not r.get("warm_up")
    ]


def _dollars(runs: list[dict[str, Any]]) -> list[float]:
    out: list[float] = []
    for r in runs:
        value = r.get("dollars")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out.append(float(value))
    return out


def _mean_dollars(records: list[dict[str, Any]], task: str, arm: str) -> float | None:
    vals = _dollars(_measured(records, task, arm))
    if not vals:
        return None
    return sum(vals) / len(vals)


def _resolve_rate(records: list[dict[str, Any]], arm: str) -> float:
    runs = [r for r in records if r.get("arm") == arm and not r.get("warm_up")]
    if not runs:
        return 0.0
    return sum(1 for r in runs if r.get("resolved")) / len(runs)


def _task_resolved(records: list[dict[str, Any]], task: str, arm: str) -> bool:
    return any(r.get("resolved") for r in _measured(records, task, arm))


def _on_mechanism_fired(run: dict[str, Any]) -> bool:
    kind = run.get("kind")
    if kind == "subagent":
        return bool(run.get("brief_fired"))
    if kind == "compact":
        return bool(run.get("carry_fired"))
    return True  # plain tasks have no mechanism: per-protocol == ITT


def _on_runs(records: list[dict[str, Any]], task: str, per_protocol: bool) -> list[dict[str, Any]]:
    runs = [r for r in _measured(records, task, "on") if not per_protocol or _on_mechanism_fired(r)]
    return runs


def _mean_on_dollars(records: list[dict[str, Any]], task: str, per_protocol: bool) -> float | None:
    vals = _dollars(_on_runs(records, task, per_protocol))
    if not vals:
        return None
    return sum(vals) / len(vals)


def _bootstrap_ci(log_ratios: list[float], seed: int, resamples: int) -> tuple[float, float]:
    if not log_ratios:
        return (math.nan, math.nan)
    rng = random.Random(seed)
    n = len(log_ratios)
    means: list[float] = []
    for _ in range(resamples):
        sample = [log_ratios[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int(0.025 * (resamples - 1))]
    hi = means[int(0.975 * (resamples - 1))]
    return (lo, hi)


def analyze(
    records: list[dict[str, Any]],
    *,
    seed: int = BOOTSTRAP_SEED,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> dict[str, Any]:
    infra_excluded = sum(1 for r in records if r.get("infra_failure"))
    records = [r for r in records if not r.get("infra_failure")]  # protocol: infrastructure only
    tasks = sorted({r["task"] for r in records})
    arms = sorted({r["arm"] for r in records})

    per_task: dict[str, Any] = {}
    log_ratios: list[float] = []
    losses: list[str] = []
    wins: list[str] = []
    unpriced: list[str] = []
    on_fail_off_pass = 0
    off_fail_on_pass = 0

    for task in tasks:
        off = _mean_dollars(records, task, "off")
        on = _mean_dollars(records, task, "on")
        on_pp = _mean_on_dollars(records, task, per_protocol=True)
        ratio = on / off if (on is not None and off is not None and off > 0) else None
        entry: dict[str, Any] = {
            "kind": next((r["kind"] for r in records if r["task"] == task), "?"),
            "off_mean_dollars": round(off, 8) if off is not None else None,
            "on_mean_dollars": round(on, 8) if on is not None else None,
            "on_mean_dollars_per_protocol": round(on_pp, 8) if on_pp is not None else None,
            "on_off_ratio": round(ratio, 4) if ratio is not None else None,
            "log_ratio": math.log(ratio) if ratio is not None and ratio > 0 else None,
        }
        if ratio is None:
            unpriced.append(task)
        else:
            log_ratios.append(math.log(ratio))
            if ratio > 1.0:
                losses.append(task)
            elif ratio < 1.0:
                wins.append(task)
        off_resolved = _task_resolved(records, task, "off")
        on_resolved = _task_resolved(records, task, "on")
        entry["off_resolved"] = off_resolved
        entry["on_resolved"] = on_resolved
        if off_resolved and not on_resolved:
            on_fail_off_pass += 1
        if on_resolved and not off_resolved:
            off_fail_on_pass += 1
        per_task[task] = entry

    geo_mean_ratio = math.exp(sum(log_ratios) / len(log_ratios)) if log_ratios else None
    ci_lo_log, ci_hi_log = _bootstrap_ci(log_ratios, seed, resamples)
    ci_lo = math.exp(ci_lo_log) if log_ratios else None
    ci_hi = math.exp(ci_hi_log) if log_ratios else None

    resolve = {arm: round(_resolve_rate(records, arm), 4) for arm in arms}

    overhead = {"hook_calls": 0, "latency_ms_total": 0.0}
    for r in records:
        ov = r.get("overhead")
        if isinstance(ov, dict):
            overhead["hook_calls"] += int(ov.get("hook_calls", 0) or 0)
            overhead["latency_ms_total"] += float(ov.get("latency_ms_total", 0.0) or 0.0)
    overhead["latency_ms_total"] = round(overhead["latency_ms_total"], 3)

    non_inferior = (on_fail_off_pass - off_fail_on_pass) <= CLAIM_NON_INFERIOR_TASKS
    cheaper = geo_mean_ratio is not None and geo_mean_ratio <= (1.0 - CLAIM_CHEAPER)
    interval_excludes_no_effect = ci_hi is not None and ci_hi < 1.0
    claim_met = bool(cheaper and interval_excludes_no_effect and non_inferior)

    return {
        "tasks": len(tasks),
        "arms": arms,
        "resolve_rate": resolve,
        "on_fail_off_pass": on_fail_off_pass,
        "off_fail_on_pass": off_fail_on_pass,
        "non_inferiority_margin_tasks": CLAIM_NON_INFERIOR_TASKS,
        "non_inferiority_met": non_inferior,
        "per_task": per_task,
        "geometric_mean_ratio": round(geo_mean_ratio, 4) if geo_mean_ratio is not None else None,
        "bootstrap_95ci": (
            [round(ci_lo, 4), round(ci_hi, 4)]
            if (ci_lo is not None and ci_hi is not None)
            else None
        ),
        "losses": losses,
        "wins": wins,
        "unpriced_tasks": unpriced,
        "overhead": overhead,
        "claim_cheaper_than": CLAIM_CHEAPER,
        "claim_met": claim_met,
        "infra_excluded": infra_excluded,
        "pilot": bool(records and records[0].get("mode") == "pilot"),
    }


def format_markdown(result: dict[str, Any]) -> str:
    label = "PILOT (not evidence)" if result["pilot"] else "confirmatory"
    lines = [f"# Plugin A/B {label}", ""]
    if result["geometric_mean_ratio"] is not None:
        lo, hi = result["bootstrap_95ci"] or (0.0, 0.0)
        lines.append(
            f"Geometric-mean on/off cost ratio: {result['geometric_mean_ratio']:.3f} "
            f"(95% bootstrap CI {lo:.3f}–{hi:.3f}, n={result['tasks']} tasks)"
        )
    else:
        lines.append("Geometric-mean on/off cost ratio: unavailable (no priced tasks)")
    if result.get("infra_excluded"):
        lines.append(
            "Excluded as infrastructure failures (decided blind to the arm): "
            f"{result['infra_excluded']} run(s)"
        )
    lines.append("")
    lines.append("## Resolve rate")
    for arm, rate in result["resolve_rate"].items():
        lines.append(f"- {arm}: {rate:.3f}")
    lines.append(f"- tasks on-fails/off-passes: {result['on_fail_off_pass']}")
    lines.append(f"- tasks off-fails/on-passes: {result['off_fail_on_pass']}")
    lines.append(
        f"- non-inferiority (≤ {result['non_inferiority_margin_tasks']} task): "
        f"{'met' if result['non_inferiority_met'] else 'NOT met'}"
    )
    lines.append("")
    lines.append("## Per-task (on/off mean dollars per non-warm-up run)")
    for task, e in result["per_task"].items():
        ratio = f"{e['on_off_ratio']:.3f}" if e["on_off_ratio"] is not None else "unpriced"
        lines.append(
            f"- {task} ({e['kind']}): ratio {ratio}, off-resolved "
            f"{e['off_resolved']}, on-resolved {e['on_resolved']}"
        )
    lines.append("")
    lines.append(f"## Losses (on costs more): {', '.join(result['losses']) or 'none'}")
    lines.append(f"## Wins (on costs less): {', '.join(result['wins']) or 'none'}")
    if result["unpriced_tasks"]:
        lines.append(f"## Unpriced (excluded from ratios): {', '.join(result['unpriced_tasks'])}")
    lines.append("")
    ov = result["overhead"]
    lines.append(
        f"## Plugin overhead: {ov['hook_calls']} hook calls, "
        f"{ov['latency_ms_total']:.1f} ms latency total"
    )
    lines.append("")
    lines.append(
        f"## Claim ({result['claim_cheaper_than']:.0%} cheaper with 95% CI excluding no effect "
        f"AND non-inferior resolve rate): {'MET' if result['claim_met'] else 'not met'}"
    )
    return "\n".join(lines)


def load_records(report_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not report_dir.is_dir():
        return records
    for path in sorted(report_dir.glob("*.json")):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        runs = obj.get("runs") if isinstance(obj, dict) else None
        if isinstance(runs, list):
            for r in runs:
                if isinstance(r, dict):
                    r = dict(r)
                    r["mode"] = obj.get("mode", "confirmatory")
                    records.append(r)
    return records


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(prog="python -m evals.plugin_ab.analyze")
    p.add_argument(
        "--report-dir",
        default=str(Path(__file__).resolve().parent.parents[1] / "evals" / "reports" / "plugin_ab"),
    )
    p.add_argument("--json", action="store_true")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    report_dir = Path(args.report_dir)
    records = load_records(report_dir)
    result = analyze(records)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_markdown(result))
        (report_dir / "analysis.md").write_text(format_markdown(result) + "\n", encoding="utf-8")
        (report_dir / "analysis.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
