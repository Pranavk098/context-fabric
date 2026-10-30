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

from evals.plugin_ab import prices as _prices

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
V2_ARMS = ("off", "shipped", "checkpoint")
V2_REPRICE_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5")
V2_THRESHOLDS: dict[str, dict[str, Any]] = {
    # short: shipped must not regress; medium/long: a real saving with a CI clear
    # of 1, no success loss beyond one run, and recall kept.
    "short": {"ratio": 1.05, "ci": False, "fail_margin": None, "recall_drop": None},
    "medium": {"ratio": 0.85, "ci": True, "fail_margin": 1, "recall_drop": 1},
    "long": {"ratio": 0.80, "ci": True, "fail_margin": 1, "recall_drop": 1},
}


def _is_slice0(record: dict[str, Any]) -> bool:
    return record.get("slice") == "slice0" or record.get("mode") == "slice0"


def _token_cost(block: Any, prices: dict[str, Any], model: str) -> float | None:
    if not isinstance(block, dict):
        return None
    cost, unpriced = _prices.dollar_cost(
        prices,
        model,
        input_tokens=int(block.get("input_tokens", 0) or 0),
        cache_creation_5m=int(block.get("cache_creation_input_tokens", 0) or 0),
        cache_creation_1h=0,
        cache_read=int(block.get("cache_read_input_tokens", 0) or 0),
        output_tokens=int(block.get("output_tokens", 0) or 0),
    )
    return None if unpriced is not None else cost


def _repriced_dollars(record: dict[str, Any], prices: dict[str, Any], model: str) -> float | None:
    """Re-price the record's token counts with another model's table."""
    steps = record.get("steps")
    blocks: list[Any] = []
    if isinstance(steps, list) and steps:
        for step in steps:
            usage = step.get("usage") if isinstance(step, dict) else None
            if not isinstance(usage, dict):
                return None
            blocks.extend((usage.get("parent"), usage.get("subagents")))
    else:
        usage = record.get("usage")
        if not isinstance(usage, dict):
            return None
        blocks.extend((usage.get("parent"), usage.get("subagents")))
    total = 0.0
    found = False
    for block in blocks:
        if block is None:
            continue
        cost = _token_cost(block, prices, model)
        if cost is None:
            return None
        total += cost
        found = True
    return total if found else None


def _record_cost(
    record: dict[str, Any], prices: dict[str, Any] | None, model: str | None
) -> float | None:
    if prices is not None and model is not None:
        value = _repriced_dollars(record, prices, model)
        if value is not None:
            return value
    dollars = record.get("dollars")
    return (
        float(dollars)
        if isinstance(dollars, (int, float)) and not isinstance(dollars, bool)
        else None
    )


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
        and not _is_slice0(r)
    ]


def _v2_runs(records: list[dict[str, Any]], stratum: str, arm: str) -> list[dict[str, Any]]:
    return [
        r
        for r in records
        if r.get("stratum") == stratum
        and r.get("arm") == arm
        and not r.get("warm_up")
        and not r.get("infra_failure")
        and not _is_slice0(r)
    ]


def _v2_values(
    records: list[dict[str, Any]],
    stratum: str,
    task: str,
    arm: str,
    prices: dict[str, Any] | None,
    model: str | None,
) -> list[float]:
    values: list[float] = []
    for record in _v2_measured(records, stratum, task, arm):
        cost = _record_cost(record, prices, model)
        if cost is not None:
            values.append(cost)
    return values


def _v2_mean_dollars(
    records: list[dict[str, Any]],
    stratum: str,
    task: str,
    arm: str,
    prices: dict[str, Any] | None = None,
    model: str | None = None,
) -> float | None:
    vals = _v2_values(records, stratum, task, arm, prices, model)
    if not vals:
        return None
    return sum(vals) / len(vals)


def _hierarchical_bootstrap(
    records: list[dict[str, Any]],
    stratum: str,
    arm: str,
    seed: int,
    resamples: int,
    prices: dict[str, Any] | None,
    model: str | None,
) -> tuple[float, float, float] | None:
    """Resample tasks, then repetitions within each task (hierarchical bootstrap).

    Returns ``(ci_lo, ci_hi, two_sided_p)``; the p-value feeds the Holm correction.
    """
    tasks = sorted({r["task"] for r in records if r.get("stratum") == stratum})
    pairs: dict[str, tuple[list[float], list[float]]] = {}
    for task in tasks:
        off = _v2_values(records, stratum, task, "off", prices, model)
        treat = _v2_values(records, stratum, task, arm, prices, model)
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
    below = sum(1 for value in logs if value < 0)
    above = sum(1 for value in logs if value > 0)
    p_value = min(1.0, 2.0 * min(below, above) / len(logs))
    logs.sort()
    lo = math.exp(logs[int(0.025 * (len(logs) - 1))])
    hi = math.exp(logs[int(0.975 * (len(logs) - 1))])
    return (lo, hi, p_value)


def _holm(p_values: dict[str, float]) -> dict[str, float]:
    """Holm step-down correction across the non-off arms within one stratum."""
    ordered = sorted(p_values.items(), key=lambda item: item[1])
    adjusted: dict[str, float] = {}
    running = 0.0
    total = len(ordered)
    for index, (arm, value) in enumerate(ordered):
        running = max(running, min(1.0, (total - index) * value))
        adjusted[arm] = running
    return adjusted


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


def _geo_ratio(
    records: list[dict[str, Any]],
    tasks: list[str],
    stratum: str,
    arm: str,
    prices: dict[str, Any],
    model: str,
) -> tuple[float | None, dict[str, Any]]:
    per_task: dict[str, Any] = {}
    log_ratios: list[float] = []
    for task in tasks:
        off = _v2_mean_dollars(records, stratum, task, "off", prices, model)
        treat = _v2_mean_dollars(records, stratum, task, arm, prices, model)
        ratio = treat / off if (off is not None and treat is not None and off > 0) else None
        per_task[task] = {
            "off_mean_dollars": round(off, 8) if off is not None else None,
            "arm_mean_dollars": round(treat, 8) if treat is not None else None,
            "ratio": round(ratio, 4) if ratio is not None else None,
        }
        if ratio is not None and ratio > 0:
            log_ratios.append(math.log(ratio))
    geo = math.exp(sum(log_ratios) / len(log_ratios)) if log_ratios else None
    return geo, per_task


def analyze_v2(
    records: list[dict[str, Any]],
    *,
    seed: int = BOOTSTRAP_SEED,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> dict[str, Any]:
    """Per-stratum report: each registered arm versus off, with claim pass/fail columns.

    Claims use the same token counts re-priced at the Opus 5.5 table (the Sonnet
    5.5 ratio is reported alongside); Haiku's 5x-cheaper sub-100K tier would
    otherwise flatter compaction savings. Records without usage fall back to the
    recorded dollars so harness-validation data stays analyzable.
    """
    infra_excluded = sum(1 for r in records if r.get("infra_failure"))
    slice0_excluded = sum(1 for r in records if _is_slice0(r))
    records = [r for r in records if not r.get("infra_failure")]
    present = {str(r.get("arm")) for r in records}
    arms = [a for a in V2_ARMS if a in present]
    prices = _prices.load_prices()
    claim_model = V2_REPRICE_MODELS[0]
    result: dict[str, Any] = {
        "arms": arms,
        "infra_excluded": infra_excluded,
        "slice0_excluded": slice0_excluded,
        "priced_as": claim_model,
        "thresholds": V2_THRESHOLDS,
        "strata": {},
    }
    for stratum in STRATA:
        tasks = sorted(
            {r["task"] for r in records if r.get("stratum") == stratum and not _is_slice0(r)}
        )
        if not tasks:
            continue
        arm_entries: dict[str, Any] = {}
        raw_p: dict[str, float] = {}
        pending: dict[str, dict[str, Any]] = {}
        for arm in arms:
            if arm == "off":
                continue
            geo, per_task = _geo_ratio(records, tasks, stratum, arm, prices, claim_model)
            sonnet_geo, _ = _geo_ratio(records, tasks, stratum, arm, prices, V2_REPRICE_MODELS[1])
            ci = _hierarchical_bootstrap(
                records, stratum, arm, seed, resamples, prices, claim_model
            )
            step_stats = _step_stats(records, stratum, arm)
            off_step_stats = _step_stats(records, stratum, "off")
            recall = _recall_stats(records, stratum, arm)
            off_recall = _recall_stats(records, stratum, "off")
            failed = _failed_runs(records, stratum, arm)
            off_failed = _failed_runs(records, stratum, "off")
            raw_p[arm] = ci[2] if ci is not None else 1.0
            pending[arm] = {
                "geometric_mean_ratio": round(geo, 4) if geo is not None else None,
                "bootstrap_95ci": ([round(ci[0], 4), round(ci[1], 4)] if ci is not None else None),
                "raw_p": round(raw_p[arm], 6),
                "repriced": {
                    "opus-5.5": round(geo, 4) if geo is not None else None,
                    "sonnet-5.5": round(sonnet_geo, 4) if sonnet_geo is not None else None,
                },
                "step_pass_rate": step_stats["rate"],
                "step_passed": step_stats["passed"],
                "step_total": step_stats["total"],
                "off_step_pass_rate": off_step_stats["rate"],
                "recall": recall,
                "off_recall": off_recall,
                "recall_drop_probes": off_recall["passed"] - recall["passed"],
                "failed_runs": failed,
                "off_failed_runs": off_failed,
                "extra_turns_after_checkpoint": _extra_turns_mean(records, stratum, arm),
                "per_task": per_task,
            }
        for arm, entry in pending.items():
            threshold = V2_THRESHOLDS[stratum]
            geo = entry["geometric_mean_ratio"]
            ci = entry["bootstrap_95ci"]
            holm_p = _holm(raw_p)[arm]
            entry["holm_p"] = round(holm_p, 6)
            entry["ratio_threshold"] = threshold["ratio"]
            entry["ratio_pass"] = geo is not None and geo <= float(threshold["ratio"])
            entry["ci_pass"] = None if not threshold["ci"] else (ci is not None and ci[1] < 1.0)
            entry["holm_pass"] = None if not threshold["ci"] else holm_p < 0.05
            entry["success_pass"] = (
                None
                if threshold["fail_margin"] is None
                else (
                    entry["failed_runs"] - entry["off_failed_runs"] <= int(threshold["fail_margin"])
                )
            )
            entry["recall_pass"] = (
                None
                if threshold["recall_drop"] is None
                else (entry["recall_drop_probes"] <= int(threshold["recall_drop"]))
            )
            entry["claim_met"] = all(
                value is True
                for value in (
                    entry["ratio_pass"],
                    entry["ci_pass"],
                    entry["holm_pass"],
                    entry["success_pass"],
                    entry["recall_pass"],
                )
                if value is not None
            )
            arm_entries[arm] = entry
        result["strata"][stratum] = {"tasks": tasks, "arms": arm_entries}
    return result


def format_markdown_v2(result: dict[str, Any]) -> str:
    lines = [
        "# Plugin A/B v2 by stratum (registered design; claims priced as Opus 5.5)",
        "",
    ]
    if result.get("infra_excluded"):
        lines.append(f"Excluded as infrastructure failures: {result['infra_excluded']} run(s)")
        lines.append("")
    if result.get("slice0_excluded"):
        lines.append(f"Excluded harness-validation slice runs: {result['slice0_excluded']}")
        lines.append("")
    for stratum in STRATA:
        block = result["strata"].get(stratum)
        if not block:
            continue
        lines.append(f"## {stratum} ({len(block['tasks'])} tasks)")
        lines.append("")
        lines.append(
            "| arm | ratio vs off | 95% CI | step pass | recall | extra turns after checkpoint "
            "| ratio | CI | Holm | success | recall | claim |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
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
                f"| {mark(entry['holm_pass'])} | {mark(entry['success_pass'])} "
                f"| {mark(entry['recall_pass'])} "
                f"| {'MET' if entry['claim_met'] else 'not met'} |"
            )
        lines.append("")
    return "\n".join(lines)


def format_markdown_v3(result: dict[str, Any]) -> str:
    lines = [
        "# Plugin A/B v3 adversarial chains (EXPLORATORY, not registered)",
        "",
        str(result["note"]),
        "",
        "| chain | arm | runs | pass | fail | killed | misleading claims |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for chain, arms in result["chains"].items():
        for arm, entry in arms.items():
            lines.append(
                f"| {chain} | {arm} | {entry['runs']} | {entry['pass']} | {entry['fail']} "
                f"| {entry['killed']} | {entry['misleading_claims']} |"
            )
    lines.append("")
    return "\n".join(lines)


def run_outcome(record: dict[str, Any]) -> str:
    """``killed`` if any step was killed, else ``pass``/``fail`` over the whole chain."""
    steps = record.get("steps")
    if not isinstance(steps, list) or not steps:
        return "fail"
    outcomes = [
        str(step.get("outcome", "pass" if step.get("resolved") else "fail"))
        for step in steps
        if isinstance(step, dict)
    ]
    if "killed" in outcomes:
        return "killed"
    return "pass" if outcomes and all(outcome == "pass" for outcome in outcomes) else "fail"


def analyze_v3(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Exploratory per-chain view: oracle outcomes and misleading claims, no intervals."""
    records = [r for r in records if not r.get("infra_failure") and not r.get("warm_up")]
    chains: dict[str, dict[str, Any]] = {}
    for record in records:
        chain = str(record.get("task"))
        arm = str(record.get("arm"))
        entry = chains.setdefault(chain, {}).setdefault(
            arm,
            {"runs": 0, "pass": 0, "fail": 0, "killed": 0, "misleading_claims": 0, "outcomes": []},
        )
        outcome = run_outcome(record)
        entry["runs"] += 1
        entry[outcome] += 1
        entry["outcomes"].append(outcome)
        steps = record.get("steps")
        if isinstance(steps, list):
            entry["misleading_claims"] += sum(
                1 for step in steps if isinstance(step, dict) and step.get("misleading_claim")
            )
    return {
        "label": "EXPLORATORY",
        "note": "EXPLORATORY: n is small; no confirmatory claim",
        "chains": chains,
    }


V4_ARMS = ("none", "brief_v0", "brief_v1", "fork", "control")
# Comparisons are (treatment, baseline). The first is the primary; the rest are secondary.
V4_PRIMARY = ("brief_v1", "none")
# No brief_v1 vs fork cost comparison: a fork child runs on the parent model (Sonnet) while the
# other arms use Haiku children, so a ratio would report a model difference as a context effect.
# The fork arm stays in the per-arm table only.
V4_SECONDARY = (("brief_v1", "brief_v0"),)


def _v4_runs(records: list[dict[str, Any]], arm: str) -> list[dict[str, Any]]:
    return [
        r
        for r in records
        if r.get("v4")
        and r.get("arm") == arm
        and not r.get("warm_up")
        and not r.get("infra_failure")
    ]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _num(runs: list[dict[str, Any]], key: str) -> list[float]:
    return [
        float(r[key])
        for r in runs
        if isinstance(r.get(key), (int, float)) and not isinstance(r.get(key), bool)
    ]


def _rate(runs: list[dict[str, Any]], pred: Any) -> float | None:
    return sum(1 for r in runs if pred(r)) / len(runs) if runs else None


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def _v4_arm_stats(runs: list[dict[str, Any]]) -> dict[str, Any]:
    facts = sum(int(r.get("planted_of") or 0) for r in runs)
    passed = sum(int(r.get("planted_passed") or 0) for r in runs)
    return {
        "runs": len(runs),
        "spawn_rate": _round(_rate(runs, lambda r: not r.get("no_spawn"))),
        "no_spawn_runs": sum(1 for r in runs if r.get("no_spawn")),
        "pass_rate": _round(_rate(runs, lambda r: bool(r.get("resolved")))),
        "hidden_pass_rate": _round(_rate(runs, lambda r: bool(r.get("hidden_pass")))),
        "planted_fact_pass_rate": _round(passed / facts if facts else None),
        "mean_dollars": _round(_mean(_num(runs, "dollars")), 6),
        "mean_child_rereads": _round(_mean(_num(runs, "child_rereads"))),
        "mean_child_compactions": _round(_mean(_num(runs, "child_compactions"))),
        "mean_child_overflows": _round(_mean(_num(runs, "child_overflows"))),
        "mean_child_parent_msg_overlap": _round(_mean(_num(runs, "child_parent_msg_overlap"))),
        "cap_hit_runs": sum(1 for r in runs if r.get("cap_hit")),
    }


def _v4_task_means(runs: list[dict[str, Any]], key: str) -> dict[str, float]:
    by_task: dict[str, list[float]] = {}
    for r in runs:
        by_task.setdefault(str(r["task"]), []).extend(_num([r], key))
    return {task: sum(v) / len(v) for task, v in by_task.items() if v}


def _task_solved(runs: list[dict[str, Any]], task: str) -> bool:
    return any(r.get("resolved") for r in runs if r["task"] == task)


def _v4_compare(
    records: list[dict[str, Any]], treat: str, base: str, seed: int, resamples: int
) -> dict[str, Any]:
    """Paired per-task cost ratio treat/base (all non-infra runs, intention to treat)."""
    t_runs, b_runs = _v4_runs(records, treat), _v4_runs(records, base)
    t_cost, b_cost = _v4_task_means(t_runs, "dollars"), _v4_task_means(b_runs, "dollars")
    log_ratios = [
        math.log(t_cost[k] / b_cost[k])
        for k in sorted(t_cost)
        if k in b_cost and t_cost[k] > 0 and b_cost[k] > 0
    ]
    geo = math.exp(sum(log_ratios) / len(log_ratios)) if log_ratios else None
    lo, hi = _bootstrap_ci(log_ratios, seed, resamples)
    tasks = sorted({str(r["task"]) for r in t_runs} & {str(r["task"]) for r in b_runs})
    treat_fails = sum(1 for k in tasks if _task_solved(b_runs, k) and not _task_solved(t_runs, k))
    base_fails = sum(1 for k in tasks if _task_solved(t_runs, k) and not _task_solved(b_runs, k))
    ts, bs = _v4_arm_stats(t_runs), _v4_arm_stats(b_runs)

    def diff(key: str) -> float | None:
        a, b = ts[key], bs[key]
        return round(a - b, 4) if a is not None and b is not None else None

    return {
        "treatment": treat,
        "baseline": base,
        "tasks": len(log_ratios),
        "geometric_mean_cost_ratio": _round(geo),
        "bootstrap_95ci": [round(math.exp(lo), 4), round(math.exp(hi), 4)] if log_ratios else None,
        "treatment_pass_rate": ts["pass_rate"],
        "baseline_pass_rate": bs["pass_rate"],
        "tasks_treatment_fails_baseline_passes": treat_fails,
        "tasks_baseline_fails_treatment_passes": base_fails,
        "non_inferior": (treat_fails - base_fails) <= CLAIM_NON_INFERIOR_TASKS,
        "non_inferiority_margin_tasks": CLAIM_NON_INFERIOR_TASKS,
        "delta_child_rereads": diff("mean_child_rereads"),
        "delta_child_compactions": diff("mean_child_compactions"),
        "delta_child_overflows": diff("mean_child_overflows"),
        "delta_planted_fact_pass_rate": diff("planted_fact_pass_rate"),
    }


def analyze_v4(
    records: list[dict[str, Any]],
    *,
    seed: int = BOOTSTRAP_SEED,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> dict[str, Any]:
    """v4 delegation report: primary brief_v1 vs none, secondary comparisons, per-arm table.

    Cost is parent plus children (dedup counted when the run was recorded). The ratio interval
    is the v1 task-level bootstrap and non-inferiority is the v1 task rule. Nothing here asserts
    significance, and the control arm is a validity check, not a comparison.
    """
    v4 = [r for r in records if r.get("v4")]
    arms = {arm: _v4_arm_stats(_v4_runs(v4, arm)) for arm in V4_ARMS if _v4_runs(v4, arm)}
    control = arms.get("control")
    return {
        "arms": arms,
        "primary": _v4_compare(v4, *V4_PRIMARY, seed, resamples),
        "secondary": [_v4_compare(v4, t, b, seed, resamples) for t, b in V4_SECONDARY],
        "control_valid": None if control is None else control["planted_fact_pass_rate"] == 0.0,
        "infra_excluded": sum(1 for r in v4 if r.get("infra_failure")),
        "pilot": any(r.get("mode") == "pilot" for r in v4),
    }


def _f(value: Any, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _ci(c: dict[str, Any]) -> str:
    ci = c["bootstrap_95ci"]
    return "n/a" if ci is None else f"{ci[0]:.3f} to {ci[1]:.3f}"


def format_markdown_v4(result: dict[str, Any]) -> str:
    label = "PILOT (not evidence)" if result["pilot"] else "delegation"
    p = result["primary"]
    lines = [
        f"# Plugin A/B v4 {label}",
        "",
        "## Primary: total cost (parent + children) at non-inferior pass rate",
        "",
        "| comparison | tasks | cost ratio | 95% bootstrap CI | pass (treat / base) "
        "| treat fails, base passes | base fails, treat passes | non-inferior |",
        "|---|---|---|---|---|---|---|---|",
        f"| {p['treatment']} vs {p['baseline']} | {p['tasks']} "
        f"| {_f(p['geometric_mean_cost_ratio'])} | {_ci(p)} "
        f"| {_f(p['treatment_pass_rate'])} / {_f(p['baseline_pass_rate'])} "
        f"| {p['tasks_treatment_fails_baseline_passes']} "
        f"| {p['tasks_baseline_fails_treatment_passes']} "
        f"| {'yes' if p['non_inferior'] else 'NO'} "
        f"(margin {p['non_inferiority_margin_tasks']} task) |",
        "",
        "## Secondary: comparisons",
        "",
        "| comparison | tasks | cost ratio | 95% CI | d child re-reads | d compactions "
        "| d overflows | d planted-fact pass |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in result["secondary"]:
        lines.append(
            f"| {c['treatment']} vs {c['baseline']} | {c['tasks']} "
            f"| {_f(c['geometric_mean_cost_ratio'])} | {_ci(c)} "
            f"| {_f(c['delta_child_rereads'])} | {_f(c['delta_child_compactions'])} "
            f"| {_f(c['delta_child_overflows'])} | {_f(c['delta_planted_fact_pass_rate'])} |"
        )
    lines += [
        "",
        "## Secondary: per arm",
        "",
        "| arm | runs | spawn rate | no_spawn | pass | hidden pass | planted-fact pass "
        "| mean $ | child re-reads | child compactions | child overflows | parent-msg overlap |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for arm, a in result["arms"].items():
        lines.append(
            f"| {arm} | {a['runs']} | {_f(a['spawn_rate'])} | {a['no_spawn_runs']} "
            f"| {_f(a['pass_rate'])} | {_f(a['hidden_pass_rate'])} "
            f"| {_f(a['planted_fact_pass_rate'])} | {_f(a['mean_dollars'], 4)} "
            f"| {_f(a['mean_child_rereads'])} | {_f(a['mean_child_compactions'])} "
            f"| {_f(a['mean_child_overflows'])} | {_f(a['mean_child_parent_msg_overlap'])} |"
        )
    valid = result["control_valid"]
    lines += [
        "",
        "Control check (planted-fact pass rate must be 0): "
        + ("no control runs" if valid is None else ("valid" if valid else "INVALID")),
    ]
    if result["infra_excluded"]:
        lines.append(f"Excluded as infrastructure failures: {result['infra_excluded']} run(s)")
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
                    if obj.get("slice") is not None:
                        r["slice"] = obj["slice"]
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
    p.add_argument(
        "--v3",
        action="store_true",
        help="exploratory per-chain report for tasks_v3_adversarial (writes analysis_v3.*)",
    )
    p.add_argument(
        "--v4",
        action="store_true",
        help="delegation report for tasks_v4 runs (writes analysis_v4.*)",
    )
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    report_dir = Path(args.report_dir)
    records = load_records(report_dir)
    if args.v4:
        result = analyze_v4(records)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            markdown = format_markdown_v4(result)
            print(markdown)
            (report_dir / "analysis_v4.md").write_text(markdown + "\n", encoding="utf-8")
            (report_dir / "analysis_v4.json").write_text(
                json.dumps(result, indent=2), encoding="utf-8"
            )
        return 0
    if args.v3:
        result = analyze_v3(records)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            markdown = format_markdown_v3(result)
            print(markdown)
            (report_dir / "analysis_v3.md").write_text(markdown + "\n", encoding="utf-8")
            (report_dir / "analysis_v3.json").write_text(
                json.dumps(result, indent=2), encoding="utf-8"
            )
        return 0
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
