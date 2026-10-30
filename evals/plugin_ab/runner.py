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
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cfxd.brief_receipts import (
    BriefReceipt,
    _scan_parent,
    join_receipts,
    path_sha16,
)
from evals.plugin_ab import prereg, v4_prompt
from evals.plugin_ab.patch import apply_patch
from evals.plugin_ab.prices import load_prices
from evals.plugin_ab.usage import ScanResult, combine, scan_transcript

_PKG = Path(__file__).resolve().parent
_REPO_ROOT = _PKG.parents[1]
DEFAULT_FIXTURE = _PKG / "fixture"
DEFAULT_TASKS = _PKG / "tasks.json"
TASKS_V2 = _PKG / "tasks_v2.json"
TASKS_V4 = _PKG / "tasks_v4.json"
DEFAULT_REPORT_DIR = _REPO_ROOT / "evals" / "reports" / "plugin_ab"

ARMS_OFF_ON = ("off", "on")
V2_ARMS = ("off", "shipped", "checkpoint")
V2_REJECTED_ARMS = ("diet", "combo")
# Recall-validation control: same session break as the checkpoint arm, but nothing is saved or
# injected, and no plugin loads. Never part of a measured comparison.
RECALL_CONTROL_ARM = "no_restore"
BOUNDARY_ARMS = ("checkpoint", RECALL_CONTROL_ARM)
RECALL_ORACLE_PREFIX = "oracles/recall_"
# The recall facts are planted in the prompt of this (0-based) step, before the first boundary.
RECALL_PLANT_STEP = 1
# The checkpoint arm saves after these 1-based step numbers and continues in a new session.
CHECKPOINT_BOUNDARIES: dict[str, tuple[int, ...]] = {"long": (4, 8), "medium": (2,), "short": ()}
# Exploratory v3 chains save after steps 2 and 4 so the harness change that follows is unseen.
V3_CHECKPOINT_BOUNDARIES: tuple[int, ...] = (3, 5)
SLICE0_MODEL = "claude-haiku-5-5"
CLAUDE_TIMEOUT_S = 1800
ORACLE_TIMEOUT_S = 300
# A step declared ``killed`` has its oracle killed this fast; the record says "killed", not failed.
KILLED_TEST_TIMEOUT_S = 0.05
# Where the runner drops the step's last assistant text for the v3 claim oracles to read.
AGENT_TEXT = Path(".harness") / "agent_text.txt"

# v4 delegation benchmark (WP H2). Every arm loads the plugin; arms differ in the brief flags
# (``V4_ARM_CONFIG``) and in how the parent is told to spawn (``fork`` inherits the conversation).
V4_ARMS = ("none", "brief_v0", "brief_v1", "fork", "control")
V4_PARENT_MODEL = "sonnet"
V4_CHILD_MODEL = "haiku"
V4_PARENT_MAX_TURNS = 40
V4_DEFAULT_CAP_USD = 1.50  # per run; EVALS_MAX_RUN_USD overrides, as for v2
V4_HIDDEN_TIMEOUT_S = 300
# carry_back stays off in every arm so the arms differ only in the handoff under test; H3 decides.
V4_ARM_CONFIG: dict[str, dict[str, bool]] = {
    "none": {"brief": False, "brief_v1": False, "carry_back": False},
    "brief_v0": {"brief": True, "brief_v1": False, "carry_back": False},
    "brief_v1": {"brief": True, "brief_v1": True, "carry_back": False},
    "fork": {"brief": False, "brief_v1": False, "carry_back": False},
    "control": {"brief": False, "brief_v1": False, "carry_back": False},
}

_ALLOWED_TOOLS = "Read,Edit,Write,Grep,Glob,Agent,Bash(python -m pytest:*)"


@dataclass(frozen=True)
class Step:
    id: str
    prompt: str
    oracle: str
    timeout: int
    reference_patch: dict[str, Any] = field(default_factory=dict)
    fake_answers: dict[str, Any] | None = None
    between: tuple[dict[str, Any], ...] = ()
    killed: bool = False
    claim_check: bool = False
    fake_text: str = ""


@dataclass(frozen=True)
class PlantedFact:
    id: str
    fact: str
    test: str  # "test_planted.py::test_name" inside the task's hidden test directory


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
    exploratory: bool = False
    # v4 (delegation benchmark) fields; empty for v1/v2/v3 tasks.
    hidden_tests: str = ""
    reference: str = ""
    reference_variant: str = ""
    planted_facts: tuple[PlantedFact, ...] = ()
    max_turns: int = 0
    timeout: int = 0


@dataclass(frozen=True)
class RunSpec:
    task_id: str
    arm: str
    rep: int  # -1 is the warm-up run
    warm_up: bool
    order_index: int
    seed: int = 0  # experiment seed; recall facts are derived from it per run


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
                between=tuple(step.get("between") or ()),
                killed=bool(step.get("killed", False)),
                claim_check=bool(step.get("claim_check", False)),
                fake_text=str(step.get("fake_text", "")),
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
                exploratory=bool(item.get("exploratory", False)),
                hidden_tests=str(item.get("hidden_tests", "")),
                reference=str(item.get("reference", "")),
                reference_variant=str(item.get("reference_variant", "")),
                planted_facts=tuple(
                    PlantedFact(str(f["id"]), str(f["fact"]), str(f["test"]))
                    for f in item.get("planted_facts", [])
                ),
                max_turns=int(item.get("max_turns", 0)),
                timeout=int(item.get("timeout", 0)),
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


def is_v4(tasks: list[Task]) -> bool:
    """The v4 delegation task file: tasks carry planted facts and hidden tests."""
    return any(t.planted_facts for t in tasks)


def is_v2(tasks: list[Task]) -> bool:
    return any(t.stratum for t in tasks) and not is_v4(tasks)


def is_v3(tasks: list[Task]) -> bool:
    """An exploratory task file marks each task; it is never part of the v2 prereg hash."""
    return any(t.exploratory for t in tasks)


def plan_v4(tasks: list[Task], arms: tuple[str, ...], reps: int, seed: int) -> list[RunSpec]:
    """The v2/v3 seeded ABBA order without warm-up runs (a warm-up per arm per task would
    cost 5x a measured rep on a Sonnet parent)."""
    measured = [s for s in plan_runs(tasks, arms, reps, seed) if not s.warm_up]
    return [replace(s, order_index=i) for i, s in enumerate(measured)]


def plan_smoke(tasks: list[Task]) -> list[RunSpec]:
    """Fake-claude smoke run: the first adversarial chain, one off and one checkpoint run."""
    task = next(t for t in tasks if any(s.between or s.killed for s in t.steps))
    arms = ("off", "checkpoint")
    return [RunSpec(task.id, arm, 0, False, index) for index, arm in enumerate(arms)]


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
    mcp_config: Path | None = None,
    max_turns: int = 25,
    max_budget_usd: str = "0.50",
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
        str(max_turns),
        "--max-budget-usd",
        max_budget_usd,
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
    if mcp_config is not None:
        # Account connectors leak in despite --setting-sources; an empty strict config blocks them
        # for every arm so no arm pays for (or races) ~490 deferred tools.
        cmd += ["--strict-mcp-config", "--mcp-config", str(mcp_config)]
    if arm not in ("off", RECALL_CONTROL_ARM):
        cmd += ["--plugin-dir", str(plugin_root)]
    return cmd


def write_empty_mcp_config(run_dir: Path) -> Path:
    path = run_dir / "empty_mcp.json"
    run_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    return path


def _isolate_mcp_env(env: dict[str, str]) -> None:
    # Found in the claude binary (not in --help): turns off claude.ai account connectors.
    env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"


def count_mcp_attachments(transcript: Path) -> tuple[int, int]:
    """Count ``deferred_tools_delta`` and ``mcp_instructions_delta`` attachments (counts only)."""
    deferred = instructions = 0
    try:
        with open(transcript, "rb") as fh:
            for raw in fh:
                try:
                    obj = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                att = obj.get("attachment") if isinstance(obj, dict) else None
                kind = att.get("type") if isinstance(att, dict) else None
                if kind == "deferred_tools_delta":
                    deferred += 1
                elif kind == "mcp_instructions_delta":
                    instructions += 1
    except OSError:
        pass
    return deferred, instructions


def mcp_churn(
    config_dir: Path, session_id: str, previous: tuple[int, int] = (0, 0), invocations: int = 1
) -> tuple[bool, tuple[int, int]]:
    """True when new MCP delta attachments exceed one per invocation start."""
    path = find_parent_transcript(config_dir, session_id)
    if path is None:
        return False, previous
    now = count_mcp_attachments(path)
    churn = now[0] - previous[0] > invocations or now[1] - previous[1] > invocations
    return churn, now


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
    """Only ``on_fold`` and the v4 arms need a config override; the rest use defaults."""
    if arm in V4_ARM_CONFIG:
        (cfx_home / "config.json").write_text(json.dumps(V4_ARM_CONFIG[arm]), encoding="utf-8")
    elif arm == "on_fold":
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


_TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_ADJECTIVES = (
    "amber", "brisk", "cobalt", "dusky", "ember", "flint", "gilded", "hazel", "ivory", "jade",
    "keen", "lunar",
)  # fmt: skip
_CODE_NOUNS = (
    "anchor", "beacon", "cipher", "delta", "engine", "falcon", "garnet", "harbor", "island",
    "jigsaw", "kettle", "lantern",
)  # fmt: skip


@dataclass(frozen=True)
class RecallQuestion:
    key: str
    ask: str
    expected: str


def make_recall_facts(seed: int, task_id: str, rep: int) -> tuple[str, tuple[RecallQuestion, ...]]:
    """Facts that exist nowhere in the repo, generated from the run seed.

    Returns ``(plant_text, questions)``. The plant text goes into one mid-session prompt; the
    questions are asked after the session break. Same ``(seed, task, rep)`` -> same facts.
    """
    rng = random.Random(f"recall:{seed}:{task_id}:{rep}")
    token = "CF-" + "".join(rng.choice(_TOKEN_ALPHABET) for _ in range(6))
    days = str(rng.randint(101, 989))
    codename = f"{rng.choice(_CODE_ADJECTIVES)}-{rng.choice(_CODE_NOUNS)}"
    decision_id = f"DEC-{rng.randint(1000, 9999)}"
    plant = (
        "Side note for later in this session (not part of the task; do not write it to any "
        f"file): the release token is {token}; {decision_id} decided that stale carts are "
        f"archived after {days} days; the incident codename is {codename}."
    )
    questions = (
        RecallQuestion("release_token", "the release token", token),
        RecallQuestion(
            "archive_days", "the number of days after which stale carts are archived", days
        ),
        RecallQuestion("incident_codename", "the incident codename", codename),
    )
    return plant, questions


def recall_prompt(questions: tuple[RecallQuestion, ...]) -> str:
    keys = "; ".join(f"`{q.key}` ({q.ask})" for q in questions)
    return (
        "Earlier in this session I mentioned three facts in a side note. Write `answers.json` "
        f"in the working directory with these keys: {keys}. Answer only from what was said "
        'in this session; do not search the repository, and use "unknown" if you do not know.'
    )


def _answer_matches(answer: object, expected: str) -> bool:
    pattern = r"(?<![A-Za-z0-9-])" + re.escape(expected) + r"(?![A-Za-z0-9-])"
    return re.search(pattern, str(answer), re.IGNORECASE) is not None


def score_recall(work: Path, questions: tuple[RecallQuestion, ...]) -> list[str]:
    """Keys of the questions answered correctly in ``answers.json`` (missing file -> none)."""
    try:
        answers = json.loads((work / "answers.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(answers, dict):
        return []
    return [q.key for q in questions if _answer_matches(answers.get(q.key, ""), q.expected)]


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


def checkpoint_block(python: str, work: Path, cfx_home: Path, port: int) -> str:
    """The block ``cfxd checkpoint show`` prints (the text a restore would inject)."""
    env = os.environ.copy()
    env["CFX_HOME"] = str(cfx_home)
    env["CFX_PORT"] = str(port)
    env["PYTHONPATH"] = str(_REPO_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    try:
        proc = subprocess.run(
            [python, "-m", "cfxd", "checkpoint", "show"],
            cwd=work,
            env=env,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", errors="replace").strip()


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


def _safe_rel(work: Path, rel: str) -> Path:
    if Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise ValueError(f"between action path must stay inside the workspace: {rel!r}")
    return work / rel


def snapshot_tree(root: Path) -> dict[str, bytes]:
    """Every fixture file's bytes, keyed by posix relative path (for ``revert``)."""
    out: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if {"__pycache__", ".pytest_cache", ".harness"} & set(path.relative_to(root).parts):
            continue
        out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


def _revert_path(work: Path, rel: str, snapshots: list[dict[str, bytes]]) -> None:
    """Restore ``rel`` to its content before the last step that changed it."""
    path = _safe_rel(work, rel)
    current = path.read_bytes() if path.is_file() else None
    for snap in reversed(snapshots):
        before = snap.get(rel)
        if before != current:
            if before is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(before)
            return


def apply_between(
    work: Path,
    ops: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    snapshots: list[dict[str, bytes]],
) -> None:
    """Apply a step's ``between`` actions in order (harness changes between sessions)."""
    for op in ops:
        kind = op.get("op")
        if kind == "write":
            path = _safe_rel(work, str(op["path"]))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(op["content"]), encoding="utf-8")
        elif kind == "rename":
            src = _safe_rel(work, str(op["from"]))
            dst = _safe_rel(work, str(op["to"]))
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.replace(dst)
        elif kind == "revert":
            _revert_path(work, str(op["path"]), snapshots)
        else:
            raise ValueError(f"unknown between op {kind!r}")


def last_assistant_text(transcript: Path) -> str:
    """The last assistant text block of a transcript (empty string if none)."""
    text = ""
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        message = obj.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if parts:
            text = "\n".join(parts)
    return text


def write_agent_text(work: Path, config_dir: Path, session_id: str) -> None:
    """Drop the step's last assistant text where a claim oracle can read it."""
    transcript = find_parent_transcript(config_dir, session_id)
    target = work / AGENT_TEXT
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(last_assistant_text(transcript) if transcript else "", encoding="utf-8")


def kill_step_oracle(python: str, work: Path, oracle: str) -> str:
    """Run the step oracle with a kill timeout; always report the outcome ``killed``."""
    try:
        subprocess.run(
            [python, "-m", "pytest", "-q", oracle],
            cwd=work,
            capture_output=True,
            timeout=KILLED_TEST_TIMEOUT_S,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except subprocess.TimeoutExpired:
        pass
    return "killed"


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
    run_parent = ScanResult()  # run-wide sums of the per-step deltas, across every session
    run_subs = ScanResult()
    run_total = ScanResult()
    duration = 0.0
    cap_hit = False
    boundaries: set[int] = set()
    if spec.arm in BOUNDARY_ARMS:
        registered = CHECKPOINT_BOUNDARIES.get(task.stratum, ())
        if task.exploratory:
            registered = V3_CHECKPOINT_BOUNDARIES
        boundaries = set(registered)
    checkpoints: list[dict[str, Any]] = []
    fresh_session = False
    pending_block = ""
    snapshots: list[dict[str, bytes]] = []
    mcp_config = write_empty_mcp_config(run_dir)
    mcp_seen: dict[str, tuple[int, int]] = {}
    has_recall = any(s.oracle.startswith(RECALL_ORACLE_PREFIX) for s in task.steps)
    plant_text, questions = (
        make_recall_facts(spec.seed, task.id, spec.rep) if has_recall else ("", ())
    )
    recall_invalid: list[str] = []

    for index, step in enumerate(task.steps):
        if index in boundaries and index < len(task.steps):
            if spec.arm == RECALL_CONTROL_ARM:
                saved = 0  # control: break the session, save nothing, inject nothing
            else:
                saved = save_checkpoint(python, work, cfx_home, port)
                if saved <= 0:
                    # A 0-byte save means the arm under test did not run; never score it degraded.
                    return _infra_record(task, spec, session_id, "save_empty")
            pending_block = checkpoint_block(python, work, cfx_home, port) if saved else ""
            session_id = _new_session_id()
            fresh_session = True
            # The new session's usage starts at zero; its cumulative totals are not a delta
            # against the old session's.
            previous_parent, previous_subs, previous_total = (
                ScanResult(),
                ScanResult(),
                ScanResult(),
            )
            checkpoints.append(
                {"after_step": index, "saved_bytes": saved, "restore_outcome": "none"}
            )
        if task.exploratory:
            # Snapshot before the step (for ``revert``), then let the harness change the
            # workspace after the checkpoint save, so the saved packet stays stale.
            snapshots.append(snapshot_tree(work))
            if index > 0:
                apply_between(work, task.steps[index - 1].between, snapshots)
        is_recall = step.oracle.startswith(RECALL_ORACLE_PREFIX)
        correct: list[str] = []
        step_prompt = recall_prompt(questions) if is_recall else step.prompt
        if plant_text and index == RECALL_PLANT_STEP:
            step_prompt = step_prompt + "\n\n" + plant_text
        cmd = build_command(
            claude_bin,
            prompt=(pending_block + chr(10) * 2 if pending_block else "") + step_prompt,
            model=model,
            session_id=session_id,
            arm=spec.arm,
            plugin_root=plugin_root,
            resume=index > 0 and not fresh_session,
            mcp_config=mcp_config,
        )
        env = os.environ.copy()
        _isolate_mcp_env(env)
        env["CFX_HOME"] = str(cfx_home)
        env["CFX_PORT"] = str(port)
        if override_config:
            env["CLAUDE_CONFIG_DIR"] = str(config_dir)
        if fake:
            env["PLUGIN_AB_FAKE_STEP"] = "run"
            solution_path = run_dir / f"solution_{index}.json"
            solution_path.write_text(json.dumps(step.reference_patch), encoding="utf-8")
            env["PLUGIN_AB_SOLUTION"] = str(solution_path)
            if is_recall:
                # The fake "remembers" unless the arm has no memory of the planted facts.
                forgets = spec.arm == RECALL_CONTROL_ARM
                fake_recall = {q.key: "unknown" if forgets else q.expected for q in questions}
                answers_path = run_dir / f"answers_{index}.json"
                answers_path.write_text(json.dumps(fake_recall), encoding="utf-8")
                env["PLUGIN_AB_FAKE_ANSWERS"] = str(answers_path)
            elif step.fake_answers is not None:
                answers_path = run_dir / f"answers_{index}.json"
                answers_path.write_text(json.dumps(step.fake_answers), encoding="utf-8")
                env["PLUGIN_AB_FAKE_ANSWERS"] = str(answers_path)
            if step.fake_text:
                env["PLUGIN_AB_FAKE_TEXT"] = step.fake_text
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
        churn, mcp_seen[session_id] = mcp_churn(
            config_dir, session_id, mcp_seen.get(session_id, (0, 0))
        )
        if churn:
            return _infra_record(task, spec, session_id, "mcp_churn")
        if fresh_session:
            if spec.arm == RECALL_CONTROL_ARM:
                checkpoints[-1]["restore_outcome"] = "none"
            else:
                checkpoints[-1]["restore_outcome"] = "injected" if pending_block else "missing"
            pending_block = ""
            fresh_session = False

        if task.exploratory:
            write_agent_text(work, config_dir, session_id)

        if step.killed:
            outcome = kill_step_oracle(python, work, step.oracle)
            recall_score, recall_of = None, None
            resolved = False
        elif is_recall:
            correct = score_recall(work, questions)
            recall_score, recall_of = len(correct), len(questions)
            resolved = recall_score == recall_of
            outcome = "pass" if resolved else "fail"
            if spec.arm == RECALL_CONTROL_ARM:
                recall_invalid = correct
        else:
            oracle_result = run_step_oracle(python, work, step.oracle, step.timeout)
            if oracle_result is None:
                return _infra_record(task, spec, session_id, "step oracle could not execute")
            resolved, recall_score, recall_of = oracle_result
            outcome = "pass" if resolved else "fail"

        try:
            parent, subs, total, sub_count = collect_session_usage(config_dir, session_id, prices)
        except RuntimeError:
            return _infra_record(task, spec, session_id, "no transcript found for the session")
        dparent = _delta_usage(previous_parent, parent)
        dsubs = _delta_usage(previous_subs, subs)
        dtotal = _delta_usage(previous_total, total)
        previous_parent, previous_subs, previous_total = parent, subs, total
        run_parent = combine([run_parent, dparent])
        run_subs = combine([run_subs, dsubs])
        run_total = combine([run_total, dtotal])

        step_record: dict[str, Any] = {
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
        if task.exploratory:
            step_record["outcome"] = outcome
            if step.claim_check:
                step_record["misleading_claim"] = not resolved
        if is_recall:
            step_record["recall_correct"] = correct
        step_records.append(step_record)
        if run_total.dollars is not None and run_total.dollars > cap_usd:
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
        "turns": run_total.turns,
        "usage": {
            "parent": _usage_dict(run_parent),
            "subagents": _usage_dict(run_subs),
            "subagent_sessions": step_records[-1]["usage"]["subagent_sessions"]
            if step_records
            else 0,
        },
        "weighted_tokens": round(run_total.weighted(), 3),
        "dollars": (round(run_total.dollars, 8) if run_total.dollars is not None else None),
        "unpriced_models": sorted(run_total.unpriced_models),
        "duration_s": round(duration, 3),
        "session_id": session_id,
    }
    if checkpoints:
        record["checkpoints"] = checkpoints
        record["boundaries_taken"] = [c["after_step"] for c in checkpoints]
        record["checkpoint_saved_bytes"] = sum(int(c["saved_bytes"]) for c in checkpoints)
        record["checkpoint_restore_outcome"] = checkpoints[-1]["restore_outcome"]
    if spec.arm == RECALL_CONTROL_ARM:
        # Any question the memoryless control answers correctly does not measure memory.
        record["recall_invalid_questions"] = recall_invalid
        record["recall_valid"] = not recall_invalid
    if spec.arm not in ("off", RECALL_CONTROL_ARM):
        record["brief_fired"] = detect_brief(cfx_home)
        record["carry_fired"] = detect_carry(cfx_home)
        record["overhead"] = collect_overhead(cfx_home)
    return record


# ----------------------------------------------------------------------------- v4 delegation

_PYTEST_OUTCOME_RE = re.compile(r"^(PASSED|FAILED|ERROR)\s+\S*::(\w+)", re.MULTILINE)


def run_hidden_tests(
    python: str, work: Path, hidden: Path, facts: tuple[PlantedFact, ...], timeout: int
) -> tuple[bool, dict[str, bool]] | None:
    """Run a v4 task's hidden tests against the workspace.

    Returns ``(behavior_pass, {fact id: planted test passed})``; ``None`` means pytest could not
    run at all (missing directory, timeout). A failing or uncollectable test is data, not infra.
    """
    if not (hidden / "test_behavior.py").is_file() or not (hidden / "test_planted.py").is_file():
        return None
    purge_pycache(work)
    env = {**os.environ, "CLICK_FIXTURE_ROOT": str(work), "PYTHONDONTWRITEBYTECODE": "1"}

    def run(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
            cwd=_REPO_ROOT,
            capture_output=True,
            timeout=timeout,
            env=env,
        )

    try:
        behavior = run(str(hidden / "test_behavior.py"))
        planted = run("-rA", str(hidden / "test_planted.py"))
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = planted.stdout.decode("utf-8", "replace")
    passed = {m.group(2) for m in _PYTEST_OUTCOME_RE.finditer(text) if m.group(1) == "PASSED"}
    return behavior.returncode == 0, {f.id: f.test.rsplit("::", 1)[-1] in passed for f in facts}


def _assistant_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                try:
                    obj = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                msg = obj.get("message") if isinstance(obj, dict) else None
                mid = msg.get("id") if isinstance(msg, dict) else None
                if obj.get("type") == "assistant" and isinstance(mid, str) and mid:
                    ids.add(mid)
    except OSError:
        pass
    return ids


def delegation_metrics(parent_path: Path, session_id: str) -> dict[str, Any]:
    """Counts about what the sub-agents read, compacted and replayed (no paths, no text).

    A child "re-read" is a distinct file the child read that the parent had already read before
    the ``Agent`` call that spawned it (path hashes, the ADR-0011 join key).
    ``child_parent_msg_overlap`` counts assistant message ids that appear in both a child and the
    parent transcript: nonzero means the child replays parent history (a fork), and those
    messages' usage would be double counted by the per-file scan.
    """
    parent = _scan_parent(parent_path)
    children = parent.children()
    agent_to_call = {aid: tid for tid, aid in parent.agent_ids.items()}
    first_call = min(parent.agent_calls.values(), default=None)
    parent_ids = _assistant_ids(parent_path)
    overlap = sum(
        len(_assistant_ids(p) & parent_ids)
        for p in find_subagent_transcripts(parent_path, session_id)
    )
    entries: list[dict[str, Any]] = []
    for aid, child in sorted(children.items()):
        tid = agent_to_call.get(aid)
        call_idx = parent.agent_calls.get(tid, first_call) if tid else first_call
        seen = {path_sha16(p) for i, p in parent.reads if call_idx is None or i < call_idx}
        read = {path_sha16(p) for p in child.reads}
        entries.append(
            {
                "model": child.model,
                "turns": child.turns,
                "compactions": child.compactions,
                "overflows": child.overflows,
                "files_read": len(read),
                "rereads_of_parent_reads": len(read & seen),
                "tokens_total": child.tokens.get("total", 0),
            }
        )
    return {
        "agent_calls": len(parent.agent_calls),
        "parent_files_read": len({path_sha16(p) for _, p in parent.reads}),
        "children": entries,
        "child_compactions": sum(int(e["compactions"]) for e in entries),
        "child_overflows": sum(int(e["overflows"]) for e in entries),
        "child_rereads": sum(int(e["rereads_of_parent_reads"]) for e in entries),
        "child_files_read": sum(int(e["files_read"]) for e in entries),
        "child_parent_msg_overlap": overlap,
        "child_models": sorted({str(e["model"]) for e in entries if e["model"]}),
    }


def receipt_summary(receipt: BriefReceipt) -> dict[str, Any]:
    """An R1 receipt reduced to counts and ids: file path lists become lengths."""
    out = receipt.to_dict()
    for key in (
        "briefed_files",
        "child_files_read",
        "rereads_of_briefed",
        "parent_read_not_briefed",
    ):
        out[f"n_{key}"] = len(out.pop(key))
    return out


def _run_v4(
    task: Task,
    spec: RunSpec,
    *,
    model: str,
    child_model: str,
    claude_bin: str,
    plugin_root: Path,
    fixture: Path,
    isolation: Path,
    prices: dict[str, Any],
    python: str,
    fake: bool,
) -> dict[str, Any]:
    """One v4 run: a Sonnet parent is told to delegate to Haiku sub-agents, then the hidden
    behaviour tests and the planted-fact tests run against the workspace."""
    session_id, run_dir, work, cfx_home, config_dir, override_config, port = _prepare_run(
        task, spec, fixture=fixture, isolation=isolation, fake=fake
    )
    configured_cap = os.environ.get("EVALS_MAX_RUN_USD")
    cap_usd = float(configured_cap) if configured_cap else V4_DEFAULT_CAP_USD
    mcp_config = write_empty_mcp_config(run_dir)
    cmd = build_command(
        claude_bin,
        prompt=v4_prompt.parent_prompt(task.prompt, spec.arm, child_model),
        model=model,
        session_id=session_id,
        arm=spec.arm,
        plugin_root=plugin_root,
        mcp_config=mcp_config,
        max_turns=V4_PARENT_MAX_TURNS,
        max_budget_usd=f"{cap_usd:.2f}",
    )
    env = os.environ.copy()
    _isolate_mcp_env(env)
    env["CFX_HOME"] = str(cfx_home)
    env["CFX_PORT"] = str(port)
    if override_config:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    for key in v4_prompt.FORK_ENV:
        env.pop(key, None)
    env.pop("CLAUDE_CODE_SUBAGENT_MODEL", None)
    if spec.arm == "fork":
        env.update(v4_prompt.FORK_ENV)  # the child inherits the parent model and conversation
    else:
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = child_model  # backs up the model named in the prompt
    if fake:
        env["PLUGIN_AB_FAKE_STEP"] = "run"
        env["PLUGIN_AB_V4_ARM"] = spec.arm
        env["PLUGIN_AB_V4_REFERENCE"] = str(_REPO_ROOT / task.reference)
        env["PLUGIN_AB_V4_READS"] = ",".join(task.needs_read_files)
        if spec.arm == "control":  # nobody knew the planted facts: the stand-in ignores them
            env["PLUGIN_AB_V4_VARIANT"] = str(_REPO_ROOT / task.reference_variant)
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd, cwd=work, env=env, capture_output=True, timeout=task.timeout or CLAUDE_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        return _infra_record(task, spec, session_id, "claude call timed out")
    duration = time.perf_counter() - start
    failure = _claude_failure(proc)
    if failure is not None:
        return _infra_record(task, spec, session_id, failure)
    if mcp_churn(config_dir, session_id)[0]:
        return _infra_record(task, spec, session_id, "mcp_churn")
    parent_path = find_parent_transcript(config_dir, session_id)
    if parent_path is None:
        return _infra_record(task, spec, session_id, "no transcript found for the session")
    hidden = run_hidden_tests(
        python, work, _REPO_ROOT / task.hidden_tests, task.planted_facts, V4_HIDDEN_TIMEOUT_S
    )
    if hidden is None:
        return _infra_record(task, spec, session_id, "hidden tests could not execute")
    behavior_pass, planted = hidden
    parent, subs, total, sub_count = collect_session_usage(config_dir, session_id, prices)
    metrics = delegation_metrics(parent_path, session_id)
    receipts: list[dict[str, Any]] = []
    briefs = cfx_home / "briefs.jsonl"
    if briefs.is_file():
        receipts = [
            receipt_summary(r)
            for r in join_receipts(
                briefs, config_dir / "projects", parent_transcripts={session_id: parent_path}
            )
        ]
    dollars = round(total.dollars, 8) if total.dollars is not None else None
    planted_pass = bool(planted) and all(planted.values())
    return {
        "task": task.id,
        "kind": task.kind,
        "stratum": task.stratum,
        "arm": spec.arm,
        "rep": spec.rep,
        "order_index": spec.order_index,
        "warm_up": spec.warm_up,
        "v4": True,
        "parent_model": model,
        "child_model_requested": None if spec.arm == "fork" else child_model,
        "facts_in_prompt": spec.arm != "control",
        "resolved": behavior_pass and planted_pass,
        "hidden_pass": behavior_pass,
        "planted": planted,
        "planted_passed": sum(1 for ok in planted.values() if ok),
        "planted_of": len(planted),
        "spawns": sub_count,
        "no_spawn": sub_count == 0,  # data, not an infrastructure failure
        "cap_hit": dollars is not None and dollars > cap_usd,
        "cap_usd": cap_usd,
        "turns": total.turns,
        "usage": {
            "parent": _usage_dict(parent),
            "subagents": _usage_dict(subs),
            "subagent_sessions": sub_count,
        },
        **metrics,
        "receipts": receipts,
        "weighted_tokens": round(total.weighted(), 3),
        "dollars": dollars,
        "unpriced_models": sorted(total.unpriced_models),
        "brief_fired": detect_brief(cfx_home),
        "carry_fired": detect_carry(cfx_home),
        "overhead": collect_overhead(cfx_home),
        "duration_s": round(duration, 3),
        "session_id": session_id,
    }


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
    child_model: str = V4_CHILD_MODEL,
) -> dict[str, Any]:
    if task.planted_facts:
        return _run_v4(
            task,
            spec,
            model=model,
            child_model=child_model,
            claude_bin=claude_bin,
            plugin_root=plugin_root,
            fixture=fixture,
            isolation=isolation,
            prices=prices,
            python=python,
            fake=fake,
        )
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
    mcp_config = write_empty_mcp_config(run_dir)
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
            mcp_config=mcp_config,
        )
        env = os.environ.copy()
        _isolate_mcp_env(env)
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

    if mcp_churn(config_dir, session_id, invocations=len(prompts))[0]:
        return _infra_record(task, spec, session_id, "mcp_churn")
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
    exploratory: bool = False,
    smoke: bool = False,
    child_model: str = V4_CHILD_MODEL,
) -> dict[str, Any]:
    if stratum:
        tasks = [t for t in tasks if t.stratum == stratum]
    v4 = is_v4(tasks)
    if smoke:
        specs = plan_smoke(tasks)
    elif v4:
        specs = plan_v4(tasks, arms, reps, seed)
    else:
        specs = plan_slice0(tasks) if slice_name == "slice0" else plan_runs(tasks, arms, reps, seed)
    if max_runs is not None:
        specs = specs[:max_runs]
    records: list[dict[str, Any]] = []
    aborted: str | None = None
    consecutive_infra = 0
    for spec in specs:
        spec = replace(spec, seed=seed)
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
            child_model=child_model,
        )
        records.append(record)
        consecutive_infra = consecutive_infra + 1 if record.get("infra_failure") else 0
        if consecutive_infra >= 2:  # stop rule from PROTOCOL.md
            aborted = "two consecutive infrastructure failures (see infra_reason in the runs)"
            break
    report = {
        "run_id": run_id,
        "mode": "exploratory" if exploratory else ("pilot" if pilot else "confirmatory"),
        "seed": seed,
        "repetitions": reps,
        "arms": list(arms),
        "model": model,
        "stratum": stratum,
        "max_run_usd": float(
            os.environ.get("EVALS_MAX_RUN_USD", V4_DEFAULT_CAP_USD if v4 else "0.60")
        ),
        "prices_as_of": prices.get("as_of"),
        "prereg_sha256": (
            None
            if (exploratory or v4)
            else (prereg.stored_hash_v2() if is_v2(tasks) else prereg.stored_hash())
        ),
        "started_at": datetime.now(UTC).isoformat(),
        "runs": records,
    }
    if v4:
        report["child_model"] = child_model
    if exploratory:
        report["exploratory"] = True
        report["label"] = "EXPLORATORY"
    if smoke:
        report["smoke"] = True
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
    p.add_argument("--task-ids", default=None, help="comma-separated task ids to run (v4 pilot)")
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
        "--exploratory",
        action="store_true",
        help="required for the v3 adversarial task file; bypasses the prereg gate",
    )
    p.add_argument(
        "--smoke",
        action="store_true",
        help="v3 only: fake claude, one adversarial chain x 2 arms (off, checkpoint)",
    )
    p.add_argument(
        "--v4",
        action="store_true",
        help="required for tasks_v4.json (delegation benchmark); pilot only until H3 freezes it",
    )
    p.add_argument(
        "--child-model", default=V4_CHILD_MODEL, help="v4 sub-agent model (default: haiku)"
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
    if args.task_ids:
        wanted = {t for t in args.task_ids.split(",") if t}
        tasks = [t for t in tasks if t.id in wanted]
        if not tasks:
            print("refusing to start: --task-ids matched no task", file=sys.stderr)
            return 2
    v2 = is_v2(tasks)
    v4 = is_v4(tasks)
    exploratory = is_v3(tasks)
    if v4 != bool(args.v4):
        print(
            "refusing to start: --v4 and the v4 task file (tasks_v4.json) go together",
            file=sys.stderr,
        )
        return 2
    if v4 and (args.slice0 or args.smoke or args.exploratory):
        print(
            "refusing to start: --slice0/--smoke/--exploratory are not v4 options", file=sys.stderr
        )
        return 2
    if exploratory and not args.exploratory:
        print(
            "refusing to start: the v3 adversarial task file is exploratory; pass --exploratory",
            file=sys.stderr,
        )
        return 2
    if args.exploratory and not exploratory:
        print(
            "refusing to start: --exploratory needs the v3 adversarial task file",
            file=sys.stderr,
        )
        return 2
    if args.smoke and not exploratory:
        print("refusing to start: --smoke is a v3 exploratory run", file=sys.stderr)
        return 2
    if args.smoke and not os.environ.get("PLUGIN_AB_CLAUDE"):
        print("refusing to start: --smoke runs the fake claude only", file=sys.stderr)
        return 2
    if v4:
        # No v4 prereg hash exists until H3 freezes the protocol: pilot runs only.
        ok, message = (pilot, "v4 is not frozen yet (H3): run it with --pilot")
    elif exploratory:
        ok, message = True, "exploratory run: no prereg gate"
    else:
        ok, message = (prereg.gate_ok_v2 if v2 else prereg.gate_ok)(_PKG, pilot)
    if not ok:
        print(f"refusing to start: {message}", file=sys.stderr)
        return 2

    claude_bin = args.claude or os.environ.get("PLUGIN_AB_CLAUDE", "claude")
    fake = bool(os.environ.get("PLUGIN_AB_CLAUDE"))
    if args.arms is not None:
        arms = tuple(a.strip() for a in args.arms.split(",") if a.strip())
    else:
        arms = V4_ARMS if v4 else (V2_ARMS if (v2 or exploratory) else ARMS_OFF_ON)
    if v4:
        unknown_v4 = [a for a in arms if a not in V4_ARMS]
        if unknown_v4:
            print(
                f"refusing to start: unknown v4 arm(s) {', '.join(unknown_v4)}; "
                f"arms are {','.join(V4_ARMS)}",
                file=sys.stderr,
            )
            return 2
    if v2 or exploratory:
        rejected = [a for a in arms if a in V2_REJECTED_ARMS]
        if rejected:
            print(
                f"refusing to start: arm(s) {', '.join(rejected)} were cut from v2; "
                "registered arms are off,shipped,checkpoint",
                file=sys.stderr,
            )
            return 2
        unknown = [a for a in arms if a not in (*V2_ARMS, RECALL_CONTROL_ARM)]
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
    if args.smoke:
        arms = ("off", "checkpoint")
    model = SLICE0_MODEL if (v2 or exploratory or args.slice0) else "haiku"
    if v4:
        model = V4_PARENT_MODEL
    if args.model is not None and not args.slice0:
        model = args.model
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    max_runs = (10 if v4 else 8) if pilot else None
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
            exploratory=exploratory,
            smoke=args.smoke,
            child_model=args.child_model,
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
