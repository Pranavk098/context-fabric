"""E-5: does the v4 instrument discriminate, without calling a model?

Scores the v4 fixture three ways against the real hidden tests:

- ``reference``: the reference solution for every task must score >= 90%.
- ``broken handoff``: the reference with the planted facts removed (each task's
  ``variant.json``) must score <= 10%.
- ``untouched fixture``: the starting fixture must score <= 10%.

A task is resolved only when all of its behavior tests and all of its planted-fact tests
pass, the same rule ``runner.py`` uses. The score is resolved tasks / total tasks, so a
handoff that drops the planted facts scores 0 even when the feature itself works.

Fixtures are copied to a temp directory; the checked-in fixtures are never modified. No
model or API call is made. Exit status is 0 only when all three bars hold.

    uv run python evals/plugin_ab/validate_v4_instrument.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from evals.plugin_ab.patch import apply_patch  # noqa: E402

_PKG = Path(__file__).resolve().parent
FIXTURE_DIR = _PKG / "fixture_v4"
REFERENCE_DIR = _PKG / "fixture_v4_reference"
HIDDEN_DIR = _PKG / "fixture_v4_hidden"
TASKS_PATH = _PKG / "tasks_v4.json"

REFERENCE_MIN = 90.0
BROKEN_MAX = 10.0
TEST_TIMEOUT_S = 300

_Prepare = Callable[[Path, dict[str, Any]], None]


@dataclass(frozen=True)
class ArmResult:
    name: str
    total: int
    behavior_passed: int
    planted_passed: int
    unresolved: tuple[str, ...]

    @property
    def resolved(self) -> int:
        return self.total - len(self.unresolved)

    @property
    def score(self) -> float:
        return 100.0 * self.resolved / self.total if self.total else 0.0


def _copy_fixture(source: Path, dest: Path) -> None:
    shutil.copytree(
        source,
        dest,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
    )


def _apply_files(dest: Path, files: Path) -> None:
    for source in sorted(files.rglob("*")):
        if source.is_file():
            target = dest / source.relative_to(files)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _prepare_reference(work: Path, task: dict[str, Any]) -> None:
    _apply_files(work, REFERENCE_DIR / task["id"] / "files")


def _prepare_broken(work: Path, task: dict[str, Any]) -> None:
    _prepare_reference(work, task)
    variant = REFERENCE_DIR / task["id"] / "variant.json"
    apply_patch(work, json.loads(variant.read_text(encoding="utf-8")))


def _prepare_untouched(work: Path, task: dict[str, Any]) -> None:
    pass


def _run_hidden(fixture: Path, test: Path) -> bool:
    """Run one hidden test file against ``fixture``; a timeout counts as a failure."""
    env = {**os.environ, "CLICK_FIXTURE_ROOT": str(fixture), "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(test)],
            cwd=_REPO,
            env=env,
            capture_output=True,
            timeout=TEST_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0


def _score_arm(
    name: str,
    tasks: list[dict[str, Any]],
    prepare: _Prepare,
    fixture: Path,
) -> ArmResult:
    behavior = planted = 0
    unresolved: list[str] = []
    for task in tasks:
        with tempfile.TemporaryDirectory(prefix="cf-v4-instrument-") as tmp:
            work = Path(tmp) / "fixture"
            _copy_fixture(fixture, work)
            prepare(work, task)
            hidden = HIDDEN_DIR / task["id"]
            behavior_ok = _run_hidden(work, hidden / "test_behavior.py")
            planted_ok = _run_hidden(work, hidden / "test_planted.py")
            behavior += behavior_ok
            planted += planted_ok
            if not (behavior_ok and planted_ok):
                unresolved.append(task["id"])
    return ArmResult(name, len(tasks), behavior, planted, tuple(unresolved))


def _checks(results: list[ArmResult]) -> list[tuple[ArmResult, str, bool]]:
    reference, broken, untouched = results
    return [
        (reference, f">={REFERENCE_MIN:.0f}%", reference.score >= REFERENCE_MIN),
        (broken, f"<={BROKEN_MAX:.0f}%", broken.score <= BROKEN_MAX),
        (untouched, f"<={BROKEN_MAX:.0f}%", untouched.score <= BROKEN_MAX),
    ]


def run_validation(fixture: Path = FIXTURE_DIR, tasks_path: Path = TASKS_PATH) -> list[ArmResult]:
    """Score the reference, broken-handoff and untouched arms on every task."""
    tasks: list[dict[str, Any]] = json.loads(tasks_path.read_text(encoding="utf-8"))
    return [
        _score_arm("reference", tasks, _prepare_reference, fixture),
        _score_arm("broken handoff", tasks, _prepare_broken, fixture),
        _score_arm("untouched fixture", tasks, _prepare_untouched, fixture),
    ]


def exit_code(results: list[ArmResult]) -> int:
    return 0 if all(ok for _, _, ok in _checks(results)) else 1


def format_report(results: list[ArmResult]) -> str:
    width = max(len(result.name) for result in results)
    lines = [
        f"{'arm':<{width}}  {'behavior':>10}  {'planted':>10}  {'score':>6}  {'bar':>6}  result"
    ]
    for result, bar, ok in _checks(results):
        behavior = f"{result.behavior_passed}/{result.total}"
        planted = f"{result.planted_passed}/{result.total}"
        lines.append(
            f"{result.name:<{width}}  {behavior:>10}  {planted:>10}  "
            f"{result.score:5.1f}%  {bar:>6}  {'pass' if ok else 'FAIL'}"
        )
    for result, bar, ok in _checks(results):
        if not ok:
            lines.append(f"bar failed: {result.name} scored {result.score:.1f}% (bar {bar})")
            if result.unresolved:
                lines.append(f"  unresolved: {', '.join(result.unresolved)}")
    lines.append("E-5 PASS" if exit_code(results) == 0 else "E-5 FAIL")
    return "\n".join(lines)


def main() -> int:
    results = run_validation()
    print(format_report(results))
    return exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
