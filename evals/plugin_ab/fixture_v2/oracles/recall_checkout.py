"""Score the long-checkout recall probe; prints {"score": n, "of": 3}."""

from __future__ import annotations

import json
from pathlib import Path


def main() -> int:
    try:
        answers = json.loads(Path("answers.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        answers = {}
    if not isinstance(answers, dict):
        answers = {}
    score = 0
    rounding = str(answers.get("rounding", "")).strip().lower()
    if "half" in rounding and "up" in rounding:
        score += 1
    if str(answers.get("error_class", "")).strip() == "CouponError":
        score += 1
    report = str(answers.get("report_format", ""))
    if "Total:" in report and "(tax" in report:
        score += 1
    print(json.dumps({"score": score, "of": 3}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
