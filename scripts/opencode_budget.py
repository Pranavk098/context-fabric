#!/usr/bin/env python3
"""Cost guard for builder agents: one line and an exit code from `opencode stats`."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

WARN_DAY = 5.0
WARN_WEEK = 15.0
STOP_DAY = 10.0
STOP_WEEK = 30.0
TIMEOUT_S = 20
COST_RE = re.compile(r"^\s*cost\s+\$([0-9]+(?:\.[0-9]+)?)\s*$", re.MULTILINE)
MESSAGES = {
    0: "continue",
    1: "prefer the cheap model and keep the wave small",
    2: "pause builder work",
}


def parse_cost(text: str) -> float | None:
    match = COST_RE.search(text)
    return float(match.group(1)) if match else None


def threshold(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def verdict(day: float, week: float) -> tuple[int, str]:
    if day >= threshold("OPENCODE_BUDGET_STOP_DAY", STOP_DAY) or week >= threshold(
        "OPENCODE_BUDGET_STOP_WEEK", STOP_WEEK
    ):
        return 2, "STOP"
    if day >= threshold("OPENCODE_BUDGET_WARN_DAY", WARN_DAY) or week >= threshold(
        "OPENCODE_BUDGET_WARN_WEEK", WARN_WEEK
    ):
        return 1, "WARN"
    return 0, "OK"


def stats_cost(binary: str, days: int) -> float | None:
    try:
        proc = subprocess.run(
            [binary, "stats", "--days", str(days), "--models", "--cost"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_cost(proc.stdout) if proc.returncode == 0 else None


def main() -> int:
    binary = shutil.which("opencode")
    if binary is None:
        print("opencode not found")
        return 0
    day = stats_cost(binary, 1)
    week = stats_cost(binary, 7)
    if day is None or week is None:
        print("opencode stats unavailable; skipping budget check")
        return 0
    code, status = verdict(day, week)
    print(
        f"{status}: ${day:.2f} today, ${week:.2f} in 7 days - {MESSAGES[code]}"
        f" (warn ${threshold('OPENCODE_BUDGET_WARN_DAY', WARN_DAY):.2f}/day"
        f" or ${threshold('OPENCODE_BUDGET_WARN_WEEK', WARN_WEEK):.2f}/week)"
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
