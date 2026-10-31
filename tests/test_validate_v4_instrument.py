"""E-5: the v4 instrument scores reference, broken handoff and untouched as claimed.

The first test runs the script on the real fixtures: the reference must clear 90%, the
reference with planted facts removed must stay at or below 10% (while its behavior tests
still pass, which is what proves the planted checks discriminate), and the untouched
fixture must stay at or below 10%. The second test tampers with a copied fixture and
proves the script exits 1 naming the failed reference bar.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.plugin_ab import validate_v4_instrument as v  # noqa: E402


def test_real_fixtures_meet_the_three_bars() -> None:
    reference, broken, untouched = v.run_validation()

    assert reference.score >= 90.0
    assert broken.score <= 10.0
    assert untouched.score <= 10.0
    assert reference.planted_passed == reference.total
    assert broken.behavior_passed == broken.total
    assert v.exit_code([reference, broken, untouched]) == 0
    assert "E-5 PASS" in v.format_report([reference, broken, untouched])


def test_tampered_copy_exits_1(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    shutil.copytree(v.FIXTURE_DIR, fixture)
    with (fixture / "src" / "click" / "__init__.py").open("a", encoding="utf-8") as fh:
        fh.write('\nraise RuntimeError("tampered")\n')

    tasks = json.loads(v.TASKS_PATH.read_text(encoding="utf-8"))[:1]
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(json.dumps(tasks), encoding="utf-8")

    results = v.run_validation(fixture=fixture, tasks_path=tasks_path)
    assert v.exit_code(results) == 1
    report = v.format_report(results)
    assert "bar failed: reference" in report
    assert "E-5 FAIL" in report
