"""Check the v4 fixture's three acceptance conditions for every task.

For each task in ``evals/plugin_ab/tasks_v4.json``:

1. hidden tests must fail on the untouched fixture;
2. hidden tests must pass after the reference file set is applied;
3. on a reference variant that ignores the planted facts, behavior tests must still
   pass and planted-fact tests must fail.

Run from the repo root:

    uv run python evals/plugin_ab/fixture_v4_hidden/verify_fixture.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
FIXTURE = HERE.parent / "fixture_v4"
TASKS = HERE.parent / "tasks_v4.json"

sys.path.insert(0, str(ROOT))

from evals.plugin_ab.patch import apply_patch  # noqa: E402


def _copy_fixture(dst: Path) -> None:
    shutil.copytree(
        FIXTURE,
        dst,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
    )


def _apply_files(fixture: Path, reference: Path) -> None:
    for source in sorted(reference.rglob("*")):
        if source.is_file():
            target = fixture / source.relative_to(reference)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _run_pytest(fixture: Path, test_path: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CLICK_FIXTURE_ROOT"] = str(fixture)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            str(test_path),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=300,
    )


def _tail(result: subprocess.CompletedProcess[str], limit: int = 2500) -> str:
    text = (result.stdout + result.stderr).strip()
    return text[-limit:]


def _check(task: dict[str, str], keep: bool) -> tuple[bool, list[str]]:
    lines: list[str] = []
    hidden = ROOT / task["hidden_tests"]
    reference = ROOT / task["reference"]
    variant = ROOT / task["reference_variant"]
    ok = True

    with tempfile.TemporaryDirectory(prefix="cf-v4-") as tmp:
        tmp_path = Path(tmp)

        untouched = tmp_path / "untouched"
        _copy_fixture(untouched)
        before = _run_pytest(untouched, hidden)
        if before.returncode == 0:
            ok = False
            lines.append("  (a) FAIL: hidden tests passed on the untouched fixture")
            lines.append(_tail(before))
        else:
            lines.append("  (a) ok: hidden tests fail on the untouched fixture")

        solved = tmp_path / "solved"
        _copy_fixture(solved)
        _apply_files(solved, reference)
        after = _run_pytest(solved, hidden)
        if after.returncode != 0:
            ok = False
            lines.append("  (b) FAIL: hidden tests fail with the reference applied")
            lines.append(_tail(after))
        else:
            lines.append("  (b) ok: hidden tests pass with the reference applied")

        ignored = tmp_path / "ignored"
        _copy_fixture(ignored)
        _apply_files(ignored, reference)
        apply_patch(ignored, json.loads(variant.read_text(encoding="utf-8")))

        behavior = _run_pytest(ignored, hidden / "test_behavior.py")
        planted = _run_pytest(ignored, hidden / "test_planted.py")

        if behavior.returncode != 0:
            ok = False
            lines.append("  (c) FAIL: behavior tests fail on the fact-ignoring variant")
            lines.append(_tail(behavior))
        elif planted.returncode == 0:
            ok = False
            lines.append("  (c) FAIL: planted tests pass on the fact-ignoring variant")
            lines.append(_tail(planted))
        else:
            lines.append("  (c) ok: behavior passes and planted tests fail on the variant")

        if keep and not ok:
            kept = ROOT / "evals/plugin_ab/.v4_debug" / task["id"]
            kept.parent.mkdir(parents=True, exist_ok=True)
            if kept.exists():
                shutil.rmtree(kept)
            shutil.copytree(ignored, kept)
            lines.append(f"  kept failing variant at {kept}")

    return ok, lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", help="run one task id")
    parser.add_argument("--keep", action="store_true", help="keep failing temp trees")
    args = parser.parse_args()

    tasks = json.loads(TASKS.read_text(encoding="utf-8"))
    if args.task:
        tasks = [task for task in tasks if task["id"] == args.task]

    if not tasks:
        print(f"no task matched {args.task!r}")
        return 1

    failures = 0
    for task in tasks:
        print(f"{task['id']}")
        ok, lines = _check(task, args.keep)
        print("\n".join(lines))
        failures += 0 if ok else 1

    print(f"\n{len(tasks) - failures}/{len(tasks)} tasks passed all three checks")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
