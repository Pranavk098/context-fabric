"""B8 result aggregator — per-arm R, O, U and McNemar, exactly as the pre-registration defines.

Reads the persistent run receipt (``evals/reports/b8_runs.jsonl``, one row per finished/
failed task x arm, appended as each run completes) and the official harness summaries
(``<model>.b8_<arm>.json`` produced by ``grade()``), then prints each arm's resolved (R),
overflow (O) and uncached-token (U) totals and the paired McNemar tests.

Rows whose exit status is ``cap_stopped``, ``parent_failed`` or ``tooling_failure`` are not
a result and are excluded (audit F6): a ``cap_stopped`` run re-runs from scratch, a
``parent_failed`` task is excluded, and a ``tooling_failure`` is re-run via the explicit
flag. The latest row per (instance, arm) is authoritative, so a re-run supersedes the
earlier attempt without double-counting.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from evals.b8_live import ARMS

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


def resolved_ids(model: str, arm: str, reports_dir: Path) -> set[str]:
    """The ``resolved_ids`` from the official per-arm summary, or empty when absent."""
    path = reports_dir / f"{model}.b8_{arm}.json"
    if not path.is_file():
        return set()
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return set(report.get("resolved_ids", []))


def aggregate(runs_path: Path, model: str, reports_dir: Path) -> dict[str, dict[str, Any]]:
    """Per-arm ``{R, O, U, resolved}`` over the finished (non-excluded) rows."""
    latest = latest_rows(_read_runs(runs_path))
    resolved_by_arm = {arm: resolved_ids(model, arm, reports_dir) for arm in ARMS}
    result: dict[str, dict[str, Any]] = {
        arm: {"R": 0, "O": 0, "U": 0, "resolved": {}, "tasks": 0} for arm in ARMS
    }
    for (instance_id, arm), row in latest.items():
        if arm not in ARMS:
            continue
        if row.get("exit_status") in _EXCLUDED:
            continue
        result[arm]["tasks"] += 1
        value = 1 if instance_id in resolved_by_arm[arm] else 0
        result[arm]["resolved"][instance_id] = value
        result[arm]["R"] += value
        if row.get("overflow"):
            result[arm]["O"] += 1
        result[arm]["U"] += int(row.get("tokens", {}).get("uncached_tokens", 0))
    return result


def mcnemar_p(n10: int, n01: int) -> float:
    """Two-sided exact McNemar p-value over the discordant pairs."""
    n = n10 + n01
    if n == 0:
        return 1.0
    k = min(n10, n01)
    tail: float = sum(math.comb(n, i) for i in range(k + 1)) / (2**n)
    return min(1.0, 2 * tail)


def mcnemar_table(result: dict[str, dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Paired McNemar on resolved outcomes for every arm pair (discordant counts + p)."""
    resolved = {arm: result[arm]["resolved"] for arm in ARMS}
    table: dict[tuple[str, str], dict[str, Any]] = {}
    for i, arm_x in enumerate(ARMS):
        for arm_y in ARMS[i + 1 :]:
            common = set(resolved[arm_x]) & set(resolved[arm_y])
            n10 = sum(
                1 for iid in common if resolved[arm_x][iid] == 1 and resolved[arm_y][iid] == 0
            )
            n01 = sum(
                1 for iid in common if resolved[arm_x][iid] == 0 and resolved[arm_y][iid] == 1
            )
            table[(arm_x, arm_y)] = {
                "n": len(common),
                "n10": n10,
                "n01": n01,
                "p": mcnemar_p(n10, n01),
            }
    return table


def _format(result: dict[str, dict[str, Any]]) -> str:
    lines = ["arm  tasks  R  O  U"]
    for arm in ARMS:
        r = result[arm]
        lines.append(f"{arm:<4} {r['tasks']:<6} {r['R']}  {r['O']}  {r['U']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.b8_aggregate",
        description="Compute B8 per-arm R/O/U and McNemar from the run JSONL + reports.",
    )
    parser.add_argument("--runs", default="evals/reports/b8_runs.jsonl")
    parser.add_argument("--reports-dir", default="evals/reports")
    parser.add_argument("--model", default="gpt-4.1-mini")
    args = parser.parse_args(argv)

    result = aggregate(Path(args.runs), args.model, Path(args.reports_dir))
    print(_format(result))
    print("\nMcNemar (paired, exact):")
    for (arm_x, arm_y), stats in mcnemar_table(result).items():
        print(
            f"  {arm_x} vs {arm_y}: n={stats['n']} discordant "
            f"({arm_x} only={stats['n10']}, {arm_y} only={stats['n01']}) p={stats['p']:.4g}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
