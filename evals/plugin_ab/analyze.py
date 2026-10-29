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


STRATA = ("short", "medium", "long")
V2_ARMS = ("off", "shipped", "diet", "checkpoint", "combo")
V2_THRESHOLDS: dict[str, dict[str, Any]] = {
    # short: shipped must not regress; medium: a real saving with a CI clear of 1;
    # long: a larger saving, no success loss beyond one run, and recall kept.
    "short": {"ratio": 1.05, "ci": False, "fail_margin": None, "recall_drop": None},
    "medium": {"ratio": 0.85, "ci": True, "fail_margin": None, "recall_drop": None},
    "long": {"ratio": 0.80, "ci": True, "fail_margin": 1, "recall_drop": 1},
}


def _v2_measured(
    records: list[dict[str, Any]], stratum: str, task: str, arm: str
) -> list[dict[str, Any]]:
    return [
        r
        for r in records
        if r.get("stratum") == stratum
        and r.get("task") == task
        and r.get("arm") == arm
        and not r.get("warm_up")
        and not r.get("infra_failure")
    ]


def _v2_runs(records: list[dict[str, Any]], stratum: str, arm: str) -> list[dict[str, Any]]:
    return [
        r
        for r in records
        if r.get("stratum") == stratum
        and r.get("arm") == arm
        and not r.get("warm_up")
        and not r.get("infra_failure")
    ]


def _v2_mean_dollars(
    records: list[dict[str, Any]], stratum: str, task: str, arm: str
) -> float | None:
    vals = _dollars(_v2_measured(records, stratum, task, arm))
    if not vals:
        return None
    return sum(vals) / len(vals)


def _hierarchical_ci(
    records: list[dict[str, Any]], stratum: str, arm: str, seed: int, resamples: int
) -> tuple[float, float] | None:
    """Resample tasks, then repetitions within each task (hierarchical bootstrap)."""
    tasks = sorted({r["task"] for r in records if r.get("stratum") == stratum})
    pairs: dict[str, tuple[list[float], list[float]]] = {}
    for task in tasks:
        off = _dollars(_v2_measured(records, stratum, task, "off"))
        treat = _dollars(_v2_measured(records, stratum, task, arm))
        if off and treat:
            pairs[task] = (off, treat)
    if not pairs:
        return None
    rng = random.Random(seed)
    keys = list(pairs)
    logs: list[float] = []
    for _ in range(resamples):
        picked = [keys[rng.randrange(len(keys))] for _ in keys]
        vals: list[float] = []
        for task in picked:
            off, treat = pairs[task]
            o = sum(off[rng.randrange(len(off))] for _ in range(len(off))) / len(off)
            t = sum(treat[rng.randrange(len(treat))] for _ in range(len(treat))) / len(treat)
            if o > 0 and t > 0:
                vals.append(math.log(t / o))
        if vals:
            logs.append(sum(vals) / len(vals))
    if not logs:
        return None
    logs.sort()
    lo = math.exp(logs[int(0.025 * (len(logs) - 1))])
    hi = math.exp(logs[int(0.975 * (len(logs) - 1))])
    return (lo, hi)


def _step_stats(records: list[dict[str, Any]], stratum: str, arm: str) -> dict[str, Any]:
    passed = total = 0
    for run in _v2_runs(records, stratum, arm):
        steps = run.get("steps")
        if isinstance(steps, list) and steps:
            passed += sum(1 for step in steps if step.get("resolved"))
            total += len(steps)
        else:
            passed += 1 if run.get("resolved") else 0
            total += 1
    return {
        "passed": passed,
        "total": total,
        "rate": round(passed / total, 4) if total else None,
    }


def _recall_stats(records: list[dict[str, Any]], stratum: str, arm: str) -> dict[str, Any]:
    runs = [
        r
        for r in _v2_runs(records, stratum, arm)
        if isinstance(r.get("recall_score"), (int, float))
        and not isinstance(r.get("recall_score"), bool)
    ]
    if not runs:
        return {"probes": 0, "passed": 0, "mean_score": None, "mean_of": None}
    passed = sum(1 for r in runs if r.get("recall_score") == r.get("recall_of"))
    scores = sum(int(r["recall_score"]) for r in runs)
    of = sum(int(r.get("recall_of") or 0) for r in runs)
    return {
        "probes": len(runs),
        "passed": passed,
        "mean_score": round(scores / len(runs), 3),
        "mean_of": round(of / len(runs), 3) if of else None,
    }


def _failed_runs(records: list[dict[str, Any]], stratum: str, arm: str) -> int:
    return sum(
        1 for r in _v2_runs(records, stratum, arm) if not r.get("resolved") or r.get("cap_hit")
    )


def _extra_turns(record: dict[str, Any]) -> int | None:
    steps = record.get("steps")
    if not isinstance(steps, list):
        return None
    for index, step in enumerate(steps):
        if step.get("checkpoint") or str(step.get("id", "")).startswith("checkpoint"):
            return sum(int(s.get("turns", 0) or 0) for s in steps[index + 1 :])
    return None


def _extra_turns_mean(records: list[dict[str, Any]], stratum: str, arm: str) -> float | None:
    values = [
        value for run in _v2_runs(records, stratum, arm) if (value := _extra_turns(run)) is not None
    ]
    return round(sum(values) / len(values), 3) if values else None


def analyze_v2(
    records: list[dict[str, Any]],
    *,
    seed: int = BOOTSTRAP_SEED,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> dict[str, Any]:
    """Per-stratum report: each arm versus off, with claim-threshold pass/fail columns."""
    infra_excluded = sum(1 for r in records if r.get("infra_failure"))
    records = [r for r in records if not r.get("infra_failure")]
    present = {str(r.get("arm")) for r in records}
    arms = [a for a in V2_ARMS if a in present] + sorted(present - set(V2_ARMS))
    result: dict[str, Any] = {
        "arms": arms,
        "infra_excluded": infra_excluded,
        "thresholds": V2_THRESHOLDS,
        "strata": {},
    }
    for stratum in STRATA:
        tasks = sorted({r["task"] for r in records if r.get("stratum") == stratum})
        if not tasks:
            continue
        arm_entries: dict[str, Any] = {}
        for arm in arms:
            if arm == "off":
                continue
            per_task: dict[str, Any] = {}
            log_ratios: list[float] = []
            for task in tasks:
                off = _v2_mean_dollars(records, stratum, task, "off")
                treat = _v2_mean_dollars(records, stratum, task, arm)
                ratio = treat / off if (off is not None and treat is not None and off > 0) else None
                per_task[task] = {
                    "off_mean_dollars": round(off, 8) if off is not None else None,
                    "arm_mean_dollars": round(treat, 8) if treat is not None else None,
                    "ratio": round(ratio, 4) if ratio is not None else None,
                }
                if ratio is not None and ratio > 0:
                    log_ratios.append(math.log(ratio))
            geo = math.exp(sum(log_ratios) / len(log_ratios)) if log_ratios else None
            ci = _hierarchical_ci(records, stratum, arm, seed, resamples)
            step_stats = _step_stats(records, stratum, arm)
            off_step_stats = _step_stats(records, stratum, "off")
            recall = _recall_stats(records, stratum, arm)
            off_recall = _recall_stats(records, stratum, "off")
            failed = _failed_runs(records, stratum, arm)
            off_failed = _failed_runs(records, stratum, "off")
            threshold = V2_THRESHOLDS[stratum]
            ratio_pass = geo is not None and geo <= float(threshold["ratio"])
            ci_pass = None if not threshold["ci"] else (ci is not None and ci[1] < 1.0)
            success_pass = (
                None
                if threshold["fail_margin"] is None
                else (failed - off_failed <= int(threshold["fail_margin"]))
            )
            recall_drop = off_recall["passed"] - recall["passed"]
            recall_pass = (
                None
                if threshold["recall_drop"] is None
                else (recall_drop <= int(threshold["recall_drop"]))
            )
            claim_met = all(
                value is True
                for value in (ratio_pass, ci_pass, success_pass, recall_pass)
                if value is not None
            )
            arm_entries[arm] = {
                "geometric_mean_ratio": round(geo, 4) if geo is not None else None,
                "bootstrap_95ci": ([round(ci[0], 4), round(ci[1], 4)] if ci is not None else None),
                "ratio_threshold": threshold["ratio"],
                "ratio_pass": ratio_pass,
                "ci_pass": ci_pass,
                "success_pass": success_pass,
                "recall_pass": recall_pass,
                "claim_met": claim_met,
                "step_pass_rate": step_stats["rate"],
                "step_passed": step_stats["passed"],
                "step_total": step_stats["total"],
                "off_step_pass_rate": off_step_stats["rate"],
                "recall": recall,
                "off_recall": off_recall,
                "recall_drop_probes": recall_drop,
                "failed_runs": failed,
                "off_failed_runs": off_failed,
                "extra_turns_after_checkpoint": _extra_turns_mean(records, stratum, arm),
                "per_task": per_task,
            }
        result["strata"][stratum] = {"tasks": tasks, "arms": arm_entries}
    return result


def format_markdown_v2(result: dict[str, Any]) -> str:
    lines = ["# Plugin A/B v2 by stratum (DRAFT, not evidence)", ""]
    if result.get("infra_excluded"):
        lines.append(f"Excluded as infrastructure failures: {result['infra_excluded']} run(s)")
        lines.append("")
    for stratum in STRATA:
        block = result["strata"].get(stratum)
        if not block:
            continue
        lines.append(f"## {stratum} ({len(block['tasks'])} tasks)")
        lines.append("")
        lines.append(
            "| arm | ratio vs off | 95% CI | step pass | recall | extra turns after checkpoint "
            "| ratio | CI | success | recall | claim |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for arm, entry in block["arms"].items():
            ratio = (
                f"{entry['geometric_mean_ratio']:.3f}"
                if entry["geometric_mean_ratio"] is not None
                else "n/a"
            )
            ci = (
                f"{entry['bootstrap_95ci'][0]:.3f}-{entry['bootstrap_95ci'][1]:.3f}"
                if entry["bootstrap_95ci"]
                else "n/a"
            )
            step = (
                f"{entry['step_pass_rate']:.3f}" if entry["step_pass_rate"] is not None else "n/a"
            )
            recall = entry["recall"]
            recall_text = (
                f"{recall['mean_score']}/{recall['mean_of']} "
                f"({recall['passed']}/{recall['probes']})"
                if recall["probes"]
                else "n/a"
            )
            extra = (
                f"{entry['extra_turns_after_checkpoint']:.1f}"
                if entry["extra_turns_after_checkpoint"] is not None
                else "n/a"
            )

            def mark(value: bool | None) -> str:
                return "n/a" if value is None else ("pass" if value else "FAIL")

            lines.append(
                f"| {arm} | {ratio} | {ci} | {step} | {recall_text} | {extra} "
                f"| {mark(entry['ratio_pass'])} | {mark(entry['ci_pass'])} "
                f"| {mark(entry['success_pass'])} | {mark(entry['recall_pass'])} "
                f"| {'MET' if entry['claim_met'] else 'not met'} |"
            )
        lines.append("")
    return "\n".join(lines)


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

    for stream in (sys.stdout, sys.stderr):  # a legacy Windows console cannot print every symbol
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    p = argparse.ArgumentParser(prog="python -m evals.plugin_ab.analyze")
    p.add_argument(
        "--report-dir",
        default=str(Path(__file__).resolve().parent.parents[1] / "evals" / "reports" / "plugin_ab"),
    )
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--v2",
        action="store_true",
        help="per-stratum report for tasks_v2 chains (writes analysis_v2.*)",
    )
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    report_dir = Path(args.report_dir)
    records = load_records(report_dir)
    if args.v2:
        result = analyze_v2(records)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            markdown = format_markdown_v2(result)
            print(markdown)
            (report_dir / "analysis_v2.md").write_text(markdown + "\n", encoding="utf-8")
            (report_dir / "analysis_v2.json").write_text(
                json.dumps(result, indent=2), encoding="utf-8"
            )
        return 0
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
