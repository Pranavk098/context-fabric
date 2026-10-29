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
import uuid
from dataclasses import dataclass, field
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
TASKS_V2 = _PKG / "tasks_v2.json"
DEFAULT_REPORT_DIR = _REPO_ROOT / "evals" / "reports" / "plugin_ab"

ARMS_OFF_ON = ("off", "on")
V2_ARMS = ("off", "shipped", "checkpoint")
V2_REJECTED_ARMS = ("diet", "combo")
# The checkpoint arm saves after these 1-based step numbers and continues in a new session.
CHECKPOINT_BOUNDARIES: dict[str, tuple[int, ...]] = {"long": (4, 8), "medium": (2,), "short": ()}
SLICE0_MODEL = "claude-haiku-5-5"
CLAUDE_TIMEOUT_S = 1800
ORACLE_TIMEOUT_S = 300

_ALLOWED_TOOLS = "Read,Edit,Write,Grep,Glob,Agent,Bash(python -m pytest:*)"


@dataclass(frozen=True)
class Step:
    id: str
    prompt: str
    oracle: str
    timeout: int
    reference_patch: dict[str, Any] = field(default_factory=dict)
    fake_answers: dict[str, Any] | None = None


@dataclass(frozen=True)
class Task:
    id: str
    kind: str
    prompt: str
    setup_patch: dict[str, Any]
    oracle: str
    reference_patch: dict[str, Any]
    needs_read_files: list[str]
    stratum: str = ""
    steps: tuple[Step, ...] = ()
    fixture: str = "fixture"


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
        steps = tuple(
            Step(
                id=str(step["id"]),
                prompt=str(step["prompt"]),
                oracle=str(step["oracle"]),
                timeout=int(step.get("timeout", ORACLE_TIMEOUT_S)),
                reference_patch=step.get("reference_patch") or {},
                fake_answers=step.get("fake_answers"),
            )
            for step in item.get("steps", [])
        )
        tasks.append(
            Task(
                id=str(item["id"]),
                kind=str(item["kind"]),
                prompt=str(item.get("prompt", "")),
                setup_patch=item.get("setup_patch") or {},
                oracle=str(item.get("oracle", "")),
                reference_patch=item.get("reference_patch") or {},
                needs_read_files=[str(p) for p in item.get("needs_read_files", [])],
                stratum=str(item.get("stratum", "")),
                steps=steps,
                fixture=str(item.get("fixture", "fixture")),
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


def is_v2(tasks: list[Task]) -> bool:
    return any(t.stratum for t in tasks)


def plan_slice0(tasks: list[Task]) -> list[RunSpec]:
    """Harness-validation slice: first long task in registered order, 1 off warm-up,
    then ABBA of off/checkpoint (2 measured reps each)."""
    task = next(t for t in tasks if t.stratum == "long")
    specs = [RunSpec(task.id, "off", -1, True, 0)]
    order = ("off", "checkpoint", "checkpoint", "off")
    for index, arm in enumerate(order):
        specs.append(RunSpec(task.id, arm, (0, 0, 1, 1)[index], False, index + 1))
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
        # Keep the login but do not load user-scope settings or plugins, so a plugin the user
        # installs later can never leak into the "off" arm (a fresh config dir would log out).
        "--setting-sources",
        "project,local",
    ]
    cmd += ["--resume", session_id] if resume else ["--session-id", session_id]
    if arm != "off":
        cmd += ["--plugin-dir", str(plugin_root)]
    return cmd


def _new_session_id() -> str:
    """Claude Code requires ``--session-id`` to be a UUID, and an id can be used only once."""
    return str(uuid.uuid4())


def _infra_record(task: Task, spec: RunSpec, session_id: str, reason: str) -> dict[str, Any]:
    """A run that did not produce data (launch failure, login error, missing transcript).

    The protocol excludes only infrastructure failures, decided blind to the arm; a run that
    ran but did not solve the task is data, never an exclusion.
    """
    return {
        "task": task.id,
        "kind": task.kind,
        "stratum": task.stratum,
        "arm": spec.arm,
        "rep": spec.rep,
        "order_index": spec.order_index,
        "warm_up": spec.warm_up,
        "infra_failure": True,
        "infra_reason": reason[:160],
        "session_id": session_id,
    }


def _claude_failure(proc: subprocess.CompletedProcess[bytes]) -> str | None:
    """Return a short reason when the claude call failed, else None (no model text kept)."""
    out = proc.stdout.decode("utf-8", "replace").strip().splitlines()
    if out:
        try:
            obj = json.loads(out[-1])
        except ValueError:
            obj = None
        if isinstance(obj, dict) and obj.get("is_error") is True:
            return str(obj.get("result") or "claude reported an error")
    if proc.returncode != 0:
        return f"claude exited with code {proc.returncode}"
    return None


def _claude_home(*, fake: bool, run_dir: Path) -> tuple[Path, bool]:
    """Where transcripts live, and whether to override ``CLAUDE_CONFIG_DIR`` for this run.

    The test stand-in writes into an isolated directory. A real ``claude`` must keep the
    user's own config home or it is logged out.
    """
    if fake:
        return run_dir / "claude", True
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(configured) if configured else Path.home() / ".claude"), False


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


def _task_fixture(task: Task, default: Path) -> Path:
    """A v2 task may name its own fixture directory next to the default one."""
    if task.fixture not in ("", "fixture"):
        return default.parent / task.fixture
    return default


def _write_arm_config(arm: str, cfx_home: Path) -> None:
    """Only ``on_fold`` needs a config override; every other plugin arm uses defaults."""
    if arm == "on_fold":
        (cfx_home / "config.json").write_text(
            json.dumps({"brief": True, "carry": True, "fold": True, "fold_read": True}),
            encoding="utf-8",
        )


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


def _last_receipt(cfx_home: Path, event: str) -> dict[str, Any] | None:
    path = cfx_home / "checkpoint.jsonl"
    last: dict[str, Any] | None = None
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("event") == event:
            last = obj
    return last


def save_checkpoint(python: str, work: Path, cfx_home: Path, port: int) -> int:
    """Save through the ``/context-fabric:checkpoint`` code path; return the byte count."""
    env = os.environ.copy()
    env["CFX_HOME"] = str(cfx_home)
    env["CFX_PORT"] = str(port)
    # The run cwd is the fixture copy, so put the repo on the import path for cfxd.
    env["PYTHONPATH"] = str(_REPO_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    try:
        subprocess.run(
            [python, "-m", "cfxd", "checkpoint", "save"],
            cwd=work,
            env=env,
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    receipt = _last_receipt(cfx_home, "checkpoint_save")
    if receipt is None or receipt.get("outcome") != "saved":
        return 0
    size = receipt.get("bytes")
    return int(size) if isinstance(size, int) and not isinstance(size, bool) else 0


def restore_outcome(cfx_home: Path) -> str:
    """The daemon receipt's outcome for the last checkpoint restore (counts only)."""
    receipt = _last_receipt(cfx_home, "checkpoint_restore")
    outcome = receipt.get("outcome") if receipt is not None else None
    return outcome if isinstance(outcome, str) else "none"


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


def run_oracle(python: str, work: Path, oracle: str, timeout: int = ORACLE_TIMEOUT_S) -> bool:
    purge_pycache(work)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [python, "-m", "pytest", "-q", oracle],
        cwd=work,
        capture_output=True,
        timeout=timeout,
        env=env,
    )
    return proc.returncode == 0


def purge_pycache(root: Path) -> None:
    """Drop ``__pycache__`` so an oracle run recompiles the current sources, never
    a bytecode cache left by a previous (broken) run within the same second."""
    for path in root.rglob("__pycache__"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)


def _prepare_run(
    task: Task,
    spec: RunSpec,
    *,
    fixture: Path,
    isolation: Path,
    fake: bool,
) -> tuple[str, Path, Path, Path, Path, bool, int]:
    session_id = _new_session_id()
    run_dir = isolation / f"{task.id}__{spec.arm}__{spec.rep}"
    work = run_dir / "work"
    cfx_home = run_dir / "cfx"
    config_dir, override_config = _claude_home(fake=fake, run_dir=run_dir)
    _copy_fixture(_task_fixture(task, fixture), work)
    apply_patch(work, task.setup_patch)
    cfx_home.mkdir(parents=True, exist_ok=True)
    if override_config:
        config_dir.mkdir(parents=True, exist_ok=True)
    port = free_port()
    _write_arm_config(spec.arm, cfx_home)
    return session_id, run_dir, work, cfx_home, config_dir, override_config, port


def _delta_usage(before: ScanResult, after: ScanResult) -> ScanResult:
    delta = ScanResult()
    delta.input_tokens = after.input_tokens - before.input_tokens
    delta.output_tokens = after.output_tokens - before.output_tokens
    delta.cache_creation_input_tokens = (
        after.cache_creation_input_tokens - before.cache_creation_input_tokens
    )
    delta.cache_read_input_tokens = after.cache_read_input_tokens - before.cache_read_input_tokens
    delta.turns = after.turns - before.turns
    if after.dollars is not None and before.dollars is not None:
        delta.dollars = after.dollars - before.dollars
    else:
        delta.dollars = None
    delta.unpriced_models = after.unpriced_models | before.unpriced_models
    return delta


def run_step_oracle(
    python: str, work: Path, oracle: str, timeout: int
) -> tuple[bool, int | None, int | None] | None:
    """Return ``(resolved, recall_score, recall_of)``; ``None`` means the oracle could not run."""
    if oracle.startswith("oracles/") and oracle.endswith(".py"):
        path = work / oracle
        if not path.is_file():
            return None
        try:
            proc = subprocess.run(
                [python, oracle],
                cwd=work,
                capture_output=True,
                timeout=timeout,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except subprocess.TimeoutExpired:
            return None
        if proc.returncode != 0:
            return None
        lines = proc.stdout.decode("utf-8", "replace").strip().splitlines()
        if not lines:
            return None
        try:
            obj = json.loads(lines[-1])
            score = int(obj["score"])
            of = int(obj["of"])
        except (ValueError, KeyError, TypeError):
            return None
        return (score == of, score, of)
    return (run_oracle(python, work, oracle, timeout=timeout), None, None)


def _run_chain(
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
    session_id, run_dir, work, cfx_home, config_dir, override_config, port = _prepare_run(
        task, spec, fixture=fixture, isolation=isolation, fake=fake
    )
    cap_usd = float(os.environ.get("EVALS_MAX_RUN_USD", "0.60"))
    step_records: list[dict[str, Any]] = []
    previous_parent = ScanResult()
    previous_subs = ScanResult()
    previous_total = ScanResult()
    duration = 0.0
    cap_hit = False
    boundaries = (
        set(CHECKPOINT_BOUNDARIES.get(task.stratum, ())) if spec.arm == "checkpoint" else set()
    )
    checkpoints: list[dict[str, Any]] = []
    fresh_session = False

    for index, step in enumerate(task.steps):
        if index in boundaries and index < len(task.steps):
            saved = save_checkpoint(python, work, cfx_home, port)
            session_id = _new_session_id()
            fresh_session = True
            checkpoints.append(
                {"after_step": index, "saved_bytes": saved, "restore_outcome": "none"}
            )
        cmd = build_command(
            claude_bin,
            prompt=step.prompt,
            model=model,
            session_id=session_id,
            arm=spec.arm,
            plugin_root=plugin_root,
            resume=index > 0 and not fresh_session,
        )
        env = os.environ.copy()
        env["CFX_HOME"] = str(cfx_home)
        env["CFX_PORT"] = str(port)
        # The restore handler runs inside the daemon, which starts with the first step's
        # environment, so the checkpoint arm sets this for every step (nothing is saved
        # before the first boundary, so step 1 restores nothing).
        if spec.arm == "checkpoint":
            env["CFX_RESTORE_ON_STARTUP"] = "1"
        if override_config:
            env["CLAUDE_CONFIG_DIR"] = str(config_dir)
        if fake:
            env["PLUGIN_AB_FAKE_STEP"] = "run"
            solution_path = run_dir / f"solution_{index}.json"
            solution_path.write_text(json.dumps(step.reference_patch), encoding="utf-8")
            env["PLUGIN_AB_SOLUTION"] = str(solution_path)
            if step.fake_answers is not None:
                answers_path = run_dir / f"answers_{index}.json"
                answers_path.write_text(json.dumps(step.fake_answers), encoding="utf-8")
                env["PLUGIN_AB_FAKE_ANSWERS"] = str(answers_path)
        start = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd, cwd=work, env=env, capture_output=True, timeout=CLAUDE_TIMEOUT_S
            )
        except subprocess.TimeoutExpired:
            return _infra_record(task, spec, session_id, "claude call timed out")
        step_duration = time.perf_counter() - start
        duration += step_duration
        failure = _claude_failure(proc)
        if failure is not None:
            return _infra_record(task, spec, session_id, failure)
        if fresh_session:
            checkpoints[-1]["restore_outcome"] = restore_outcome(cfx_home)
            fresh_session = False

        oracle_result = run_step_oracle(python, work, step.oracle, step.timeout)
        if oracle_result is None:
            return _infra_record(task, spec, session_id, "step oracle could not execute")
        resolved, recall_score, recall_of = oracle_result

        try:
            parent, subs, total, sub_count = collect_session_usage(config_dir, session_id, prices)
        except RuntimeError:
            return _infra_record(task, spec, session_id, "no transcript found for the session")
        dparent = _delta_usage(previous_parent, parent)
        dsubs = _delta_usage(previous_subs, subs)
        dtotal = _delta_usage(previous_total, total)
        previous_parent, previous_subs, previous_total = parent, subs, total

        step_records.append(
            {
                "id": step.id,
                "resolved": resolved,
                "recall_score": recall_score,
                "recall_of": recall_of,
                "turns": dtotal.turns,
                "dollars": round(dtotal.dollars, 8) if dtotal.dollars is not None else None,
                "usage": {
                    "parent": _usage_dict(dparent),
                    "subagents": _usage_dict(dsubs),
                    "subagent_sessions": sub_count,
                },
                "duration_s": round(step_duration, 3),
            }
        )
        if total.dollars is not None and total.dollars > cap_usd:
            cap_hit = True
            break

    resolved_all = len(step_records) == len(task.steps) and all(s["resolved"] for s in step_records)
    recall_scores = [s["recall_score"] for s in step_records if s["recall_score"] is not None]
    record: dict[str, Any] = {
        "task": task.id,
        "kind": task.kind,
        "stratum": task.stratum,
        "arm": spec.arm,
        "rep": spec.rep,
        "order_index": spec.order_index,
        "warm_up": spec.warm_up,
        "resolved": resolved_all,
        "cap_hit": cap_hit,
        "cap_usd": cap_usd,
        "steps": step_records,
        "recall_score": sum(recall_scores) if recall_scores else None,
        "recall_of": sum(s["recall_of"] or 0 for s in step_records if s["recall_of"] is not None)
        if recall_scores
        else None,
        "turns": previous_total.turns,
        "usage": {
            "parent": _usage_dict(previous_parent),
            "subagents": _usage_dict(previous_subs),
            "subagent_sessions": step_records[-1]["usage"]["subagent_sessions"]
            if step_records
            else 0,
        },
        "weighted_tokens": round(previous_total.weighted(), 3),
        "dollars": (
            round(previous_total.dollars, 8) if previous_total.dollars is not None else None
        ),
        "unpriced_models": sorted(previous_total.unpriced_models),
        "duration_s": round(duration, 3),
        "session_id": session_id,
    }
    if checkpoints:
        record["checkpoints"] = checkpoints
        record["boundaries_taken"] = [c["after_step"] for c in checkpoints]
        record["checkpoint_saved_bytes"] = sum(int(c["saved_bytes"]) for c in checkpoints)
        record["checkpoint_restore_outcome"] = checkpoints[-1]["restore_outcome"]
    if spec.arm != "off":
        record["brief_fired"] = detect_brief(cfx_home)
        record["carry_fired"] = detect_carry(cfx_home)
        record["overhead"] = collect_overhead(cfx_home)
    return record


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
    if task.steps:
        return _run_chain(
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

    session_id, run_dir, work, cfx_home, config_dir, override_config, port = _prepare_run(
        task, spec, fixture=fixture, isolation=isolation, fake=fake
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
        if override_config:
            env["CLAUDE_CONFIG_DIR"] = str(config_dir)
        if fake:
            env["PLUGIN_AB_FAKE_STEP"] = step
            if task.kind == "subagent":
                env["PLUGIN_AB_FAKE_SUBAGENT"] = "1"
            if solution_path is not None:
                env["PLUGIN_AB_SOLUTION"] = str(solution_path)
        start = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd, cwd=work, env=env, capture_output=True, timeout=CLAUDE_TIMEOUT_S
            )
        except subprocess.TimeoutExpired:
            return _infra_record(task, spec, session_id, "claude call timed out")
        duration += time.perf_counter() - start
        failure = _claude_failure(proc)
        if failure is not None:
            return _infra_record(task, spec, session_id, failure)

    resolved = run_oracle(python, work, task.oracle)
    try:
        parent, subs, total, sub_count = collect_session_usage(config_dir, session_id, prices)
    except RuntimeError:
        return _infra_record(task, spec, session_id, "no transcript found for the session")

    record: dict[str, Any] = {
        "task": task.id,
        "kind": task.kind,
        "stratum": task.stratum,
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
    stratum: str | None = None,
    slice_name: str | None = None,
) -> dict[str, Any]:
    if stratum:
        tasks = [t for t in tasks if t.stratum == stratum]
    specs = plan_slice0(tasks) if slice_name == "slice0" else plan_runs(tasks, arms, reps, seed)
    if max_runs is not None:
        specs = specs[:max_runs]
    records: list[dict[str, Any]] = []
    aborted: str | None = None
    consecutive_infra = 0
    for spec in specs:
        task = next(t for t in tasks if t.id == spec.task_id)
        record = run_one(
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
        records.append(record)
        consecutive_infra = consecutive_infra + 1 if record.get("infra_failure") else 0
        if consecutive_infra >= 2:  # stop rule from PROTOCOL.md
            aborted = "two consecutive infrastructure failures (see infra_reason in the runs)"
            break
    report = {
        "run_id": run_id,
        "mode": "pilot" if pilot else "confirmatory",
        "seed": seed,
        "repetitions": reps,
        "arms": list(arms),
        "model": model,
        "stratum": stratum,
        "max_run_usd": float(os.environ.get("EVALS_MAX_RUN_USD", "0.60")),
        "prices_as_of": prices.get("as_of"),
        "prereg_sha256": (prereg.stored_hash_v2() if is_v2(tasks) else prereg.stored_hash()),
        "started_at": datetime.now(UTC).isoformat(),
        "runs": records,
    }
    if slice_name is not None:
        report["slice"] = slice_name
    if aborted is not None:
        report["aborted"] = aborted
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
    p.add_argument("--model", default=None, help="default: haiku (v1), claude-haiku-5-5 (v2)")
    p.add_argument(
        "--claude", default=None, help="claude binary (default: PLUGIN_AB_CLAUDE or 'claude')"
    )
    p.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    p.add_argument("--run-id", default=None)
    p.add_argument(
        "--pilot", action="store_true", help="bypass the prereg gate; capped at 8 runs; PILOT label"
    )
    p.add_argument("--with-fold", action="store_true", help="add a third on_fold arm")
    p.add_argument(
        "--arms",
        default=None,
        help="comma-separated arm labels; every arm except off loads the plugin "
        "(v1 default: off,on; v2 default: off,shipped,checkpoint)",
    )
    p.add_argument(
        "--slice0",
        action="store_true",
        help="harness-validation slice: first long task, 1 off warm-up + ABBA off/checkpoint",
    )
    p.add_argument(
        "--stratum",
        choices=("short", "medium", "long"),
        default=None,
        help="only run tasks in this v2 stratum",
    )
    p.add_argument("--keep", action="store_true", help="do not delete the isolation temp dir")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # a legacy Windows console cannot print every symbol
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    pilot = bool(args.pilot)
    tasks = load_tasks(Path(args.tasks))
    v2 = is_v2(tasks)
    ok, message = (prereg.gate_ok_v2 if v2 else prereg.gate_ok)(_PKG, pilot)
    if not ok:
        print(f"refusing to start: {message}", file=sys.stderr)
        return 2

    claude_bin = args.claude or os.environ.get("PLUGIN_AB_CLAUDE", "claude")
    fake = bool(os.environ.get("PLUGIN_AB_CLAUDE"))
    if args.arms is not None:
        arms = tuple(a.strip() for a in args.arms.split(",") if a.strip())
    else:
        arms = V2_ARMS if v2 else ARMS_OFF_ON
    if v2:
        rejected = [a for a in arms if a in V2_REJECTED_ARMS]
        if rejected:
            print(
                f"refusing to start: arm(s) {', '.join(rejected)} were cut from v2; "
                "registered arms are off,shipped,checkpoint",
                file=sys.stderr,
            )
            return 2
        unknown = [a for a in arms if a not in V2_ARMS]
        if unknown:
            print(
                f"refusing to start: unknown v2 arm(s) {', '.join(unknown)}; "
                "registered arms are off,shipped,checkpoint",
                file=sys.stderr,
            )
            return 2
    if args.with_fold and "on_fold" not in arms:
        arms += ("on_fold",)
    if args.slice0:
        if not v2:
            print("refusing to start: --slice0 is a v2 slice", file=sys.stderr)
            return 2
        arms = ("off", "checkpoint")
    model = SLICE0_MODEL if (v2 or args.slice0) else "haiku"
    if args.model is not None and not args.slice0:
        model = args.model
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    max_runs = 8 if pilot else None
    isolation = Path(tempfile.mkdtemp(prefix="plugin_ab_"))
    try:
        report = run_experiment(
            tasks,
            arms=arms,
            reps=args.repetitions,
            seed=args.seed,
            model=model,
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
            stratum=args.stratum,
            slice_name="slice0" if args.slice0 else None,
        )
    finally:
        if not args.keep:
            shutil.rmtree(isolation, ignore_errors=True)
    print(json.dumps({"run_id": run_id, "report_dir": str(Path(args.report_dir))}))
    if report.get("aborted"):
        print(f"stopped early: {report['aborted']}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
