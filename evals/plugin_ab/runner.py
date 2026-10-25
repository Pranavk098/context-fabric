"""Paired A/B runner: plugin on vs off on a toy fixture, with a prereg gate.

For each ``(task, arm, repetition)`` cell the runner copies the fixture into an
isolated temp working copy, applies the task's ``setup_patch`` (the broken
state), and invokes ``claude -p`` (or the ``PLUGIN_AB_CLAUDE`` stand-in) with a
fresh ``--session-id`` and an isolated ``CFX_HOME`` / free ``CFX_PORT`` /
``CLAUDE_CONFIG_DIR``. After the run it executes the task's oracle, then reads
usage out of the transcripts (parent plus every sub-agent file) and writes one
record per run to ``evals/reports/plugin_ab/<run_id>.json``.

Records hold counts and ids only: no prompt, patch, path or transcript text, so
results can be published without leaking task content.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.plugin_ab import prereg
from evals.plugin_ab.patch import apply_patch
from evals.plugin_ab.prices import load_prices
from evals.plugin_ab.usage import ScanResult, combine, scan_transcript

_PKG = Path(__file__).resolve().parent
_REPO_ROOT = _PKG.parents[1]
DEFAULT_FIXTURE = _PKG / "fixture"
DEFAULT_TASKS = _PKG / "tasks.json"
DEFAULT_REPORT_DIR = _REPO_ROOT / "evals" / "reports" / "plugin_ab"

ARMS_OFF_ON = ("off", "on")
CLAUDE_TIMEOUT_S = 1800
ORACLE_TIMEOUT_S = 300

_ALLOWED_TOOLS = "Read,Edit,Write,Grep,Glob,Agent,Bash(python -m pytest:*)"


@dataclass(frozen=True)
class Task:
    id: str
    kind: str
    prompt: str
    setup_patch: dict[str, Any]
    oracle: str
    reference_patch: dict[str, Any]
    needs_read_files: list[str]


@dataclass(frozen=True)
class RunSpec:
    task_id: str
    arm: str
    rep: int  # -1 is the warm-up run
    warm_up: bool
    order_index: int


def load_tasks(path: Path = DEFAULT_TASKS) -> list[Task]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, list):
        raise ValueError("tasks.json must be a JSON array")
    tasks = []
    for item in obj:
        if not isinstance(item, dict):
            raise ValueError("each task must be an object")
        tasks.append(
            Task(
                id=str(item["id"]),
                kind=str(item["kind"]),
                prompt=str(item["prompt"]),
                setup_patch=item["setup_patch"],
                oracle=str(item["oracle"]),
                reference_patch=item["reference_patch"],
                needs_read_files=[str(p) for p in item["needs_read_files"]],
            )
        )
    return tasks


def plan_runs(tasks: list[Task], arms: tuple[str, ...], reps: int, seed: int) -> list[RunSpec]:
    """Seeded, ABBA-balanced run order: tasks shuffled, per-task arm order flipped by a
    coin, one warm-up run per arm, then ``reps`` measured runs in forward/reverse order."""
    rng = random.Random(seed)
    order = list(tasks)
    rng.shuffle(order)
    specs: list[RunSpec] = []
    idx = 0
    for task in order:
        seq = list(arms)
        if rng.random() < 0.5:
            seq.reverse()
        for arm in seq:
            specs.append(RunSpec(task.id, arm, -1, True, idx))
            idx += 1
        for rep in range(reps):
            pair = seq if rep % 2 == 0 else list(reversed(seq))
            for arm in pair:
                specs.append(RunSpec(task.id, arm, rep, False, idx))
                idx += 1
    return specs


def free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _claude_argv(claude_bin: str) -> list[str]:
    # The test stand-in is a .py script; the real claude is a binary on PATH.
    return [sys.executable, claude_bin] if claude_bin.endswith(".py") else [claude_bin]


def build_command(
    claude_bin: str,
    *,
    prompt: str,
    model: str,
    session_id: str,
    arm: str,
    plugin_root: Path,
    resume: bool = False,
) -> list[str]:
    cmd = [
        *_claude_argv(claude_bin),
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "json",
        "--max-turns",
        "25",
        "--max-budget-usd",
        "0.50",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        _ALLOWED_TOOLS,
    ]
    cmd += ["--resume", session_id] if resume else ["--session-id", session_id]
    if arm != "off":
        cmd += ["--plugin-dir", str(plugin_root)]
    return cmd


def _steps_for(task: Task) -> tuple[list[str], list[str]]:
    if task.kind == "compact":
        return (
            [
                f"{task.prompt}\n\nFirst, read the relevant files and write out a plan. "
                "Do not edit any files yet.",
                "/compact",
                f"{task.prompt}\n\nNow implement the change and verify it with the tests.",
            ],
            ["plan", "compact", "implement"],
        )
    return [task.prompt], ["run"]


def _copy_fixture(fixture: Path, dest: Path) -> None:
    shutil.copytree(fixture, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))


def find_parent_transcript(config_dir: Path, session_id: str) -> Path | None:
    root = config_dir / "projects"
    if not root.is_dir():
        return None
    for path in root.glob(f"**/{session_id}.jsonl"):
        return path
    return None


def find_subagent_transcripts(parent: Path, session_id: str) -> list[Path]:
    d = parent.parent / session_id / "subagents"
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.jsonl") if p.is_file())


def _log_has_decision(path: Path, decision: str) -> bool:
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("decision") == decision:
            return True
    return False


def detect_brief(cfx_home: Path) -> bool:
    return _log_has_decision(cfx_home / "briefs.jsonl", "briefed")


def detect_carry(cfx_home: Path) -> bool:
    return _log_has_decision(cfx_home / "carry.jsonl", "rendered")


def collect_overhead(cfx_home: Path) -> dict[str, float]:
    path = cfx_home / "events.jsonl"
    calls = 0
    latency_ms = 0.0
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            calls += 1
            value = obj.get("latency_ms")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                latency_ms += float(value)
    return {"hook_calls": calls, "latency_ms_total": round(latency_ms, 3)}


def _usage_dict(r: ScanResult) -> dict[str, Any]:
    return {
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "cache_creation_input_tokens": r.cache_creation_input_tokens,
        "cache_read_input_tokens": r.cache_read_input_tokens,
        "turns": r.turns,
        "weighted_tokens": round(r.weighted(), 3),
    }


def collect_session_usage(
    config_dir: Path, session_id: str, prices: dict[str, Any]
) -> tuple[ScanResult, ScanResult, ScanResult, int]:
    parent_path = find_parent_transcript(config_dir, session_id)
    if parent_path is None:
        raise RuntimeError(f"no transcript found for session {session_id}")
    parent = scan_transcript(parent_path, prices)
    sub_paths = find_subagent_transcripts(parent_path, session_id)
    subs = combine([scan_transcript(p, prices) for p in sub_paths])
    total = combine([parent, subs])
    return parent, subs, total, len(sub_paths)


def run_oracle(python: str, work: Path, oracle: str) -> bool:
    purge_pycache(work)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [python, "-m", "pytest", "-q", oracle],
        cwd=work,
        capture_output=True,
        timeout=ORACLE_TIMEOUT_S,
        env=env,
    )
    return proc.returncode == 0


def purge_pycache(root: Path) -> None:
    """Drop ``__pycache__`` so an oracle run recompiles the current sources, never
    a bytecode cache left by a previous (broken) run within the same second."""
    for path in root.rglob("__pycache__"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)


def run_one(
    task: Task,
    spec: RunSpec,
    *,
    model: str,
    claude_bin: str,
    plugin_root: Path,
    fixture: Path,
    isolation: Path,
    prices: dict[str, Any],
    python: str,
    fake: bool,
) -> dict[str, Any]:
    session_id = f"ab-{task.id}-{spec.arm}-{spec.rep}"
    run_dir = isolation / f"{task.id}__{spec.arm}__{spec.rep}"
    work = run_dir / "work"
    cfx_home = run_dir / "cfx"
    config_dir = run_dir / "claude"
    _copy_fixture(fixture, work)
    apply_patch(work, task.setup_patch)
    cfx_home.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    port = free_port()

    if spec.arm == "on_fold":
        (cfx_home / "config.json").write_text(
            json.dumps({"brief": True, "carry": True, "fold": True, "fold_read": True}),
            encoding="utf-8",
        )

    solution_path: Path | None = None
    if fake:
        solution_path = work / ".ab_solution.json"
        solution_path.write_text(json.dumps(task.reference_patch), encoding="utf-8")

    prompts, steps = _steps_for(task)
    duration = 0.0
    for prompt, step in zip(prompts, steps, strict=True):
        cmd = build_command(
            claude_bin,
            prompt=prompt,
            model=model,
            session_id=session_id,
            arm=spec.arm,
            plugin_root=plugin_root,
            resume=step in ("compact", "implement"),
        )
        env = os.environ.copy()
        env["CFX_HOME"] = str(cfx_home)
        env["CFX_PORT"] = str(port)
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
        if fake:
            env["PLUGIN_AB_FAKE_STEP"] = step
            if task.kind == "subagent":
                env["PLUGIN_AB_FAKE_SUBAGENT"] = "1"
            if solution_path is not None:
                env["PLUGIN_AB_SOLUTION"] = str(solution_path)
        start = time.perf_counter()
        subprocess.run(cmd, cwd=work, env=env, capture_output=True, timeout=CLAUDE_TIMEOUT_S)
        duration += time.perf_counter() - start

    resolved = run_oracle(python, work, task.oracle)
    parent, subs, total, sub_count = collect_session_usage(config_dir, session_id, prices)

    record: dict[str, Any] = {
        "task": task.id,
        "kind": task.kind,
        "arm": spec.arm,
        "rep": spec.rep,
        "order_index": spec.order_index,
        "warm_up": spec.warm_up,
        "resolved": resolved,
        "turns": parent.turns,
        "usage": {
            "parent": _usage_dict(parent),
            "subagents": _usage_dict(subs),
            "subagent_sessions": sub_count,
        },
        "weighted_tokens": round(total.weighted(), 3),
        "dollars": round(total.dollars, 8) if total.dollars is not None else None,
        "unpriced_models": sorted(total.unpriced_models),
        "duration_s": round(duration, 3),
        "session_id": session_id,
    }
    if spec.arm != "off":
        record["brief_fired"] = detect_brief(cfx_home)
        record["carry_fired"] = detect_carry(cfx_home)
        record["overhead"] = collect_overhead(cfx_home)
    return record


def run_experiment(
    tasks: list[Task],
    *,
    arms: tuple[str, ...],
    reps: int,
    seed: int,
    model: str,
    claude_bin: str,
    plugin_root: Path,
    fixture: Path,
    prices: dict[str, Any],
    python: str,
    fake: bool,
    pilot: bool,
    run_id: str,
    report_dir: Path,
    isolation: Path,
    max_runs: int | None = None,
) -> dict[str, Any]:
    specs = plan_runs(tasks, arms, reps, seed)
    if max_runs is not None:
        specs = specs[:max_runs]
    records: list[dict[str, Any]] = []
    for spec in specs:
        task = next(t for t in tasks if t.id == spec.task_id)
        records.append(
            run_one(
                task,
                spec,
                model=model,
                claude_bin=claude_bin,
                plugin_root=plugin_root,
                fixture=fixture,
                isolation=isolation,
                prices=prices,
                python=python,
                fake=fake,
            )
        )
    report = {
        "run_id": run_id,
        "mode": "pilot" if pilot else "confirmatory",
        "seed": seed,
        "repetitions": reps,
        "arms": list(arms),
        "model": model,
        "prices_as_of": prices.get("as_of"),
        "prereg_sha256": prereg.stored_hash(),
        "started_at": datetime.now(UTC).isoformat(),
        "runs": records,
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / f"{run_id}.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    return report


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m evals.plugin_ab.runner",
        description="Paired plugin A/B benchmark runner.",
    )
    p.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    p.add_argument("--tasks", default=str(DEFAULT_TASKS))
    p.add_argument("--repetitions", type=int, default=3)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--model", default="haiku")
    p.add_argument(
        "--claude", default=None, help="claude binary (default: PLUGIN_AB_CLAUDE or 'claude')"
    )
    p.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    p.add_argument("--run-id", default=None)
    p.add_argument(
        "--pilot", action="store_true", help="bypass the prereg gate; capped at 8 runs; PILOT label"
    )
    p.add_argument("--with-fold", action="store_true", help="add a third on_fold arm")
    p.add_argument("--keep", action="store_true", help="do not delete the isolation temp dir")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    pilot = bool(args.pilot)
    ok, message = prereg.gate_ok(_PKG, pilot)
    if not ok:
        print(f"refusing to start: {message}", file=sys.stderr)
        return 2

    claude_bin = args.claude or os.environ.get("PLUGIN_AB_CLAUDE", "claude")
    fake = bool(os.environ.get("PLUGIN_AB_CLAUDE"))
    arms = ARMS_OFF_ON + (("on_fold",) if args.with_fold else ())
    tasks = load_tasks(Path(args.tasks))
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    max_runs = 8 if pilot else None
    isolation = Path(tempfile.mkdtemp(prefix="plugin_ab_"))
    try:
        run_experiment(
            tasks,
            arms=arms,
            reps=args.repetitions,
            seed=args.seed,
            model=args.model,
            claude_bin=claude_bin,
            plugin_root=_REPO_ROOT,
            fixture=Path(args.fixture),
            prices=load_prices(),
            python=sys.executable,
            fake=fake,
            pilot=pilot,
            run_id=run_id,
            report_dir=Path(args.report_dir),
            isolation=isolation,
            max_runs=max_runs,
        )
    finally:
        if not args.keep:
            shutil.rmtree(isolation, ignore_errors=True)
    print(json.dumps({"run_id": run_id, "report_dir": str(Path(args.report_dir))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
