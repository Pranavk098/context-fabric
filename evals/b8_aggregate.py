"""B8 result aggregator — per-arm R, O, U, §5 verdicts and exact McNemar.

Reads the persistent run receipt (``b8_runs.jsonl``, one row per finished/failed task x arm,
appended as each run completes) and the official harness summaries
(``<model>.b8v3_<arm>.json`` produced by ``grade()``), then prints each arm's resolved (R),
overflow (O) and uncached-token (U) totals, the paired §5 verdicts and the exact McNemar
tests.

Only the frozen sample ids count (audit B2): a smoke or stray row must not land in R/O/U.
R, O and U are shown over each arm's own finished set, and computed for the §5 verdicts and
McNemar over the paired intersection of tasks finished in both arms of each comparison
(audit N1). A finished row with no official grade, or graded in ``error_ids``, is a tooling
failure rather than a silent 0 (audit N1). Rows whose exit status is ``cap_stopped``,
``parent_failed`` or ``tooling_failure`` are not a result (audit F6). The latest row per
(instance, arm) is authoritative, so a re-run supersedes the earlier attempt.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from evals.b8_live import ARMS, FROZEN_INSTANCE_IDS, grader_run_id

_EXCLUDED = {"cap_stopped", "parent_failed", "tooling_failure"}


def _read_runs(path: Path) -> list[dict[str, Any]]:
    """All run rows from the JSONL, skipping blank/malformed lines."""
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def latest_rows(runs: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """The latest row per ``(instance_id, arm)`` (later lines win)."""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in runs:
        instance_id = row.get("instance_id")
        arm = row.get("arm")
        if isinstance(instance_id, str) and isinstance(arm, str):
            latest[(instance_id, arm)] = row
    return latest


def _official_report(
    model: str, arm: str, reports_dir: Path
) -> tuple[set[str], set[str], set[str]]:
    """``(resolved_ids, error_ids, graded_ids)`` from the official per-arm summary.

    ``graded_ids`` is ``completed_ids ∪ empty_patch_ids`` — the instances that have a valid
    result (resolved or unresolved). An instance outside it, or inside ``error_ids``, has no
    official grade.
    """
    path = reports_dir / f"{model}.{grader_run_id(arm)}.json"
    report: dict[str, Any] = {}
    if path.is_file():
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {}
    resolved = {iid for iid in report.get("resolved_ids", []) if isinstance(iid, str)}
    error = {iid for iid in report.get("error_ids", []) if isinstance(iid, str)}
    graded = {iid for iid in report.get("completed_ids", []) if isinstance(iid, str)}
    graded |= {iid for iid in report.get("empty_patch_ids", []) if isinstance(iid, str)}
    return resolved, error, graded


def _totals(outcomes: dict[str, dict[str, Any]]) -> dict[str, int]:
    """R, O, U and task count over a set of per-instance outcomes."""
    return {
        "R": sum(1 for outcome in outcomes.values() if outcome["resolved"]),
        "O": sum(1 for outcome in outcomes.values() if outcome["overflow"]),
        "U": sum(int(outcome["uncached"]) for outcome in outcomes.values()),
        "tasks": len(outcomes),
    }


def aggregate(runs_path: Path, model: str, reports_dir: Path) -> dict[str, Any]:
    """Per-arm totals over finished, graded, frozen-sample rows; plus the per-instance outcomes.

    Returns ``{"arms": {arm: totals}, "outcomes": {arm: {instance_id: outcome}},
    "tooling": {arm: {instance_id}}}`` where ``outcome`` is
    ``{"resolved", "overflow", "uncached"}`` and ``tooling`` holds the finished rows that had
    no official grade (or graded in error).
    """
    latest = latest_rows(_read_runs(runs_path))
    # audit B2: only the frozen pilot sample is a result.
    latest = {
        (instance_id, arm): row
        for (instance_id, arm), row in latest.items()
        if instance_id in FROZEN_INSTANCE_IDS
    }
    reports = {arm: _official_report(model, arm, reports_dir) for arm in ARMS}
    outcomes: dict[str, dict[str, dict[str, Any]]] = {arm: {} for arm in ARMS}
    tooling: dict[str, set[str]] = {arm: set() for arm in ARMS}
    for (instance_id, arm), row in latest.items():
        if arm not in ARMS or row.get("exit_status") in _EXCLUDED:
            continue
        resolved, error, graded = reports[arm]
        # audit N1: a finished row with no official grade, or graded in error, is a tooling
        # failure, not a silent 0.
        if instance_id in error or instance_id not in graded:
            tooling[arm].add(instance_id)
            continue
        outcomes[arm][instance_id] = {
            "resolved": instance_id in resolved,
            "overflow": bool(row.get("overflow")),
            "uncached": int(row.get("tokens", {}).get("uncached_tokens", 0)),
        }
    return {
        "arms": {arm: _totals(outcomes[arm]) for arm in ARMS},
        "outcomes": outcomes,
        "tooling": tooling,
    }


def mcnemar_p(n10: int, n01: int) -> float:
    """Two-sided exact McNemar p-value over the discordant pairs."""
    n = n10 + n01
    if n == 0:
        return 1.0
    k = min(n10, n01)
    tail: float = sum(math.comb(n, i) for i in range(k + 1)) / (2**n)
    return min(1.0, 2 * tail)


def mcnemar_table(
    outcomes: dict[str, dict[str, dict[str, Any]]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Paired exact McNemar on resolved outcomes for every arm pair (discordant counts + p)."""
    table: dict[tuple[str, str], dict[str, Any]] = {}
    for i, arm_x in enumerate(ARMS):
        for arm_y in ARMS[i + 1 :]:
            common = set(outcomes[arm_x]) & set(outcomes[arm_y])
            n10 = sum(
                1
                for iid in common
                if outcomes[arm_x][iid]["resolved"] and not outcomes[arm_y][iid]["resolved"]
            )
            n01 = sum(
                1
                for iid in common
                if not outcomes[arm_x][iid]["resolved"] and outcomes[arm_y][iid]["resolved"]
            )
            table[(arm_x, arm_y)] = {
                "n": len(common),
                "n10": n10,
                "n01": n01,
                "p": mcnemar_p(n10, n01),
            }
    return table


def _verdict(x: dict[str, int], a: dict[str, int]) -> str:
    """The §5 verdict for arm X vs arm A over their paired intersection."""
    if x["R"] >= a["R"] + 3:
        return "STRONG SIGNAL"
    if x["R"] >= a["R"] and x["O"] <= math.ceil(a["O"] / 2) and x["U"] <= 0.75 * a["U"]:
        return "SIGNAL"
    if x["R"] < a["R"] - 1:
        return "NEGATIVE"
    return "INCONCLUSIVE"


def verdicts(
    outcomes: dict[str, dict[str, dict[str, Any]]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """The §5 verdicts for B vs A and C vs A, computed on the paired intersection."""
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for arm_x in ("B", "C"):
        common = set(outcomes[arm_x]) & set(outcomes["A"])
        x_totals = _totals({iid: outcomes[arm_x][iid] for iid in common})
        a_totals = _totals({iid: outcomes["A"][iid] for iid in common})
        result[(arm_x, "A")] = {
            "n": len(common),
            "verdict": _verdict(x_totals, a_totals),
            "x": x_totals,
            "a": a_totals,
            "dropped_x": sorted(set(outcomes[arm_x]) - common),
            "dropped_a": sorted(set(outcomes["A"]) - common),
        }
    return result


def _format(agg: dict[str, Any]) -> str:
    lines = ["arm  tasks  R  O  U"]
    for arm in ARMS:
        totals = agg["arms"][arm]
        lines.append(f"{arm:<4} {totals['tasks']:<6} {totals['R']}  {totals['O']}  {totals['U']}")
    for arm in ARMS:
        failed = sorted(agg["tooling"][arm])
        if failed:
            lines.append(f"{arm}: tooling_failure (no official grade / error_ids): {failed}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.b8_aggregate",
        description="Compute B8 per-arm R/O/U, §5 verdicts and McNemar from the run JSONL.",
    )
    parser.add_argument("--runs", default="evals/reports/b8_runs.jsonl")
    parser.add_argument("--reports-dir", default="evals/reports")
    parser.add_argument("--model", default="gpt-4.1-mini")
    args = parser.parse_args(argv)

    agg = aggregate(Path(args.runs), args.model, Path(args.reports_dir))
    print(_format(agg))
    print("\nMcNemar (paired, exact):")
    for (arm_x, arm_y), stats in mcnemar_table(agg["outcomes"]).items():
        print(
            f"  {arm_x} vs {arm_y}: n={stats['n']} discordant "
            f"({arm_x} only={stats['n10']}, {arm_y} only={stats['n01']}) p={stats['p']:.4g}"
        )
    print("\nVerdicts (§5):")
    for (arm_x, arm_y), verdict in verdicts(agg["outcomes"]).items():
        print(
            f"  {arm_x} vs {arm_y}: {verdict['verdict']} "
            f"(n={verdict['n']}, R {verdict['x']['R']} vs {verdict['a']['R']}, "
            f"O {verdict['x']['O']} vs {verdict['a']['O']}, "
            f"U {verdict['x']['U']} vs {verdict['a']['U']}; "
            f"dropped {arm_x}={verdict['dropped_x']}, {arm_y}={verdict['dropped_a']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
