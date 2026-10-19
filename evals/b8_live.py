"""B8 live harness — paired sub-agent evaluation under a capped context window.

Pre-registered in ``docs/engineering/B8-TEST-DESIGN.md``. One frozen Parent localization
trace per task, then three paired sub-agent arms (mini-swe-agent through ``cfx serve``):

* **Arm A** — naive raw file dump (FIFO-truncated at the 8,192-token hand-off slot), no fold;
* **Arm B** — current CF packet (``compile_packet``, 4-tier AST/body-strip), no fold;
* **Arm C** — BM25 full-body AST chunks + shared repo map (cartridge), plus ``--obs-fold``.

Money safety: the sidecar (single choke point) enforces the mini-tier model allow-list and
the daily token ledger; this runner refuses disallowed models before launching and reads the
same ledger for receipts. Predictions are written in SWE-bench format and graded with
``swebench.harness.run_evaluation`` in Docker.

The sampling and hand-off builders are pure and deterministic (unit-tested, no network);
``load_instances`` / ``localize_parent`` / ``run_subagent`` / ``grade`` need the WSL2 venv
(mini-swe-agent + swebench + datasets) and Docker.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import tiktoken

from evals.b4_v3 import build_ast_chunks
from evals.mech_fabric import build_blocks
from kernel.cartridge.ast_extractor import extract_source
from kernel.cartridge.serializer import Cartridge, build_cartridge, count_tokens
from kernel.router.compiler import DEFAULT_AS_OF, compile_packet
from kernel.router.recall import bm25_scores
from protocol.canonical import sha256_hex
from protocol.models import BlockKind, Recipient, Scope
from sidecar import b8
from sidecar.render import render_packet

_ENCODER = tiktoken.get_encoding("o200k_base")

WINDOW_CAP = 24_576
HANDOFF_BUDGET = 8_192
STEP_LIMIT = 35
MAX_TOKENS = 1_024
TEMPERATURE = 0.0
ARMS = ("A", "B", "C")

DATASET = "princeton-nlp/SWE-bench_Verified"
DEFAULT_SEED = 20261019
DEFAULT_N = 30

REPORTS = Path(__file__).parent / "reports"
LEDGER_DIR = REPORTS / "b8_ledger"
PARENT_CACHE = Path(__file__).parent / "parent_handoff_raw"

SCOPE = Scope(tenant_id="default", project_id="b8-live", workflow_id="swebench", task_id="t1")
RECIPIENT = Recipient(agent_id="subagent-solver", role="writer", stage="solve")

_REPO_CARTRIDGE_BUDGET = 600


def truncate_tokens(text: str, budget: int) -> str:
    """Leading bytes of ``text`` that fit ``budget`` tokens (o200k_base)."""
    if budget <= 0:
        return ""
    ids = _ENCODER.encode(text)
    if len(ids) <= budget:
        return text
    return _ENCODER.decode(ids[:budget])


# --------------------------------------------------------------------------- sampling


def sample_instances(
    instances: Sequence[dict[str, Any]], seed: int, n: int
) -> list[dict[str, Any]]:
    """Deterministic repo-stratified sample (Hamilton largest-remainder, floor >= 1).

    Canonical sort by ``instance_id``, stratify by ``repo``, allocate quotas proportional
    to repo size with a floor of 1, then draw within each stratum with
    ``random.Random(seed).sample``. Returns instances sorted by ``instance_id``.
    """
    if not instances:
        return []
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for instance in instances:
        by_repo.setdefault(instance["repo"], []).append(instance)
    repos = sorted(by_repo)
    total = len(instances)
    if n <= 0:
        return []
    # Floor of 1 only when every repo can hold a seat; small n falls back to pure Hamilton.
    floor = 1 if n >= len(repos) else 0

    exact = {repo: len(by_repo[repo]) * n / total for repo in repos}
    quotas = {repo: max(floor, int(exact[repo])) for repo in repos}
    # The floor can overshoot n for many small repos; trim the biggest quotas first.
    while sum(quotas.values()) > n:
        candidates = [repo for repo in repos if quotas[repo] > floor]
        if not candidates:
            break
        quotas[max(candidates, key=lambda repo: quotas[repo])] -= 1
    # Hamilton: hand the remaining seats to the largest fractional remainder.
    while sum(quotas.values()) < n:
        remainder = {repo: exact[repo] - quotas[repo] for repo in repos}
        quotas[max(repos, key=lambda repo: (remainder[repo], -len(by_repo[repo])))] += 1

    rng = random.Random(seed)
    chosen: list[str] = []
    for repo in repos:
        repo_ids = sorted(instance["instance_id"] for instance in by_repo[repo])
        k = min(quotas[repo], len(repo_ids))
        chosen.extend(rng.sample(repo_ids, k))

    by_id = {instance["instance_id"]: instance for instance in instances}
    return sorted((by_id[i] for i in chosen), key=lambda instance: instance["instance_id"])


# -------------------------------------------------------------------- hand-off builders


def _read(repo_root: Path, rel: str) -> str:
    return (repo_root / rel).read_text(encoding="utf-8", errors="replace")


def _repo_cartridge(repo_root: Path, files: Sequence[str], project_id: str) -> Cartridge:
    """The shared repo map (Tier-0 hologram) over ``files`` under ``repo_root``."""
    symbols = []
    hashes = []
    for rel in files:
        text = _read(repo_root, rel)
        symbols.append(extract_source(rel, text))
        hashes.append(f"{rel} {sha256_hex((repo_root / rel).read_bytes())}")
    commit = sha256_hex("\n".join(hashes).encode("utf-8"))
    return build_cartridge(
        symbols,
        tenant_id="default",
        project_id=project_id,
        commit_hash=commit,
        budget_tokens=_REPO_CARTRIDGE_BUDGET,
    )


def handoff_arm_a(
    repo_root: Path, candidate_files: Sequence[str], budget: int = HANDOFF_BUDGET
) -> str:
    """Naive raw file dump: concatenate full files in rank order, truncate at the budget."""
    parts: list[str] = []
    spent = 0
    marker = f"...[TRUNCATED AT {budget} TOKENS]"
    marker_cost = count_tokens(marker) + 4  # join separators
    for rel in candidate_files:
        block = f"===== FILE: {rel} =====\n" + _read(repo_root, rel)
        cost = count_tokens(block)
        if spent + cost > budget:
            parts.append(truncate_tokens(block, budget - spent - marker_cost))
            parts.append(marker)
            break
        parts.append(block)
        spent += cost
    return "\n\n".join(parts)


def handoff_arm_b(
    repo_root: Path,
    candidate_files: Sequence[str],
    objective: str,
    *,
    project_id: str,
    budget: int = HANDOFF_BUDGET,
) -> str:
    """Current CF packet: ``compile_packet`` (4-tier AST/body-strip) over the candidate files.

    ``compile_packet`` requires a budget divisible by 20, so the 8,192-token slot is
    rounded down to the nearest multiple of 20 (8,180) — still inside the hand-off slot.
    """
    blocks = [b for b in build_blocks(repo_root, list(candidate_files), import_statements=True)]
    blocks = [b for b in blocks if b.block_kind is not BlockKind.artifact]
    result = compile_packet(
        scope=SCOPE,
        recipient=RECIPIENT,
        objective=objective,
        stage="solve",
        blocks=blocks,
        cartridge=_repo_cartridge(repo_root, candidate_files, project_id),
        budget_base=budget - budget % 20,
        as_of=DEFAULT_AS_OF,
    )
    return render_packet(result.packet)


def handoff_arm_c(
    repo_root: Path,
    candidate_files: Sequence[str],
    objective: str,
    *,
    project_id: str,
    budget: int = HANDOFF_BUDGET,
) -> str:
    """BM25 full-body AST chunks + shared repo map (the rx_hybrid R/H2 synthesis)."""
    chunks = build_ast_chunks(repo_root, list(candidate_files))
    scores = bm25_scores(objective, chunks)
    ranked = sorted(chunks, key=lambda c: (-scores.get(c.block_id, 0.0), c.block_id))
    parts = [_repo_cartridge(repo_root, candidate_files, project_id).hologram_markdown]
    spent = count_tokens(parts[0])
    separator = count_tokens("\n\n") + 1  # join overhead per appended part
    for chunk in ranked:
        if scores.get(chunk.block_id, 0.0) <= 0.0:
            continue
        cost = count_tokens(chunk.content or "")
        if spent + cost + separator > budget:
            continue
        parts.append(chunk.content or "")
        spent += cost + separator
    return "\n\n".join(parts)


def build_handoff(
    arm: str,
    repo_root: Path,
    candidate_files: Sequence[str],
    objective: str,
    *,
    project_id: str,
) -> str:
    if arm == "A":
        return handoff_arm_a(repo_root, candidate_files)
    if arm == "B":
        return handoff_arm_b(repo_root, candidate_files, objective, project_id=project_id)
    if arm == "C":
        return handoff_arm_c(repo_root, candidate_files, objective, project_id=project_id)
    raise ValueError(f"unknown arm {arm!r}")


# ------------------------------------------------------------------------- resume / skip


def finished_pairs(predictions_path: Path) -> set[tuple[str, str]]:
    """The ``(instance_id, arm)`` pairs already present in a predictions file (or []).

    The predictions file is JSONL in SWE-bench format with an extra ``arm`` column written
    by this runner; a missing file means nothing is finished.
    """
    if not predictions_path.is_file():
        return set()
    pairs: set[tuple[str, str]] = set()
    for line in predictions_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        arm = row.get("arm")
        if isinstance(arm, str) and isinstance(row.get("instance_id"), str):
            pairs.add((row["instance_id"], arm))
    return pairs


def pending_pairs(
    instances: Sequence[dict[str, Any]], arms: Sequence[str], done: set[tuple[str, str]]
) -> list[tuple[dict[str, Any], str]]:
    """The ``(instance, arm)`` work items not yet finished."""
    return [
        (instance, arm)
        for instance in instances
        for arm in arms
        if (instance["instance_id"], arm) not in done
    ]


# ----------------------------------------------------------------------- dataset + parent


def load_instances(split: str = "test") -> list[dict[str, Any]]:
    """Load the SWE-bench Verified ``split`` (needs the ``datasets`` extra)."""
    from datasets import load_dataset  # local: WSL-only dependency

    return list(load_dataset(DATASET, split=split))


def list_python_files(repo_root: Path) -> list[str]:
    return sorted(
        path.relative_to(repo_root).as_posix()
        for path in repo_root.rglob("*.py")
        if ".git" not in path.parts
    )


def _bm25_candidate_files(
    repo_root: Path, files: Sequence[str], query: str, top_k: int = 5
) -> list[str]:
    """Deterministic BM25 fallback localization: top files by chunk score, first-wins."""
    chunks = build_ast_chunks(repo_root, list(files))
    scores = bm25_scores(query, chunks)
    ranked = sorted(chunks, key=lambda c: (-scores.get(c.block_id, 0.0), c.block_id))
    seen: list[str] = []
    for chunk in ranked:
        if scores.get(chunk.block_id, 0.0) <= 0.0:
            continue
        if chunk.ref_uri and chunk.ref_uri not in seen:
            seen.append(chunk.ref_uri)
        if len(seen) >= top_k:
            break
    return seen


def _parse_parent_json(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from a parent-model reply, if any."""
    start = text.find("{")
    while start != -1:
        depth = 0
        for end in range(start, len(text)):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start : end + 1])
                    except ValueError:
                        break
                    return value if isinstance(value, dict) else None
        start = text.find("{", start + 1)
    return None


def localize_parent(
    instance: dict[str, Any],
    repo_root: Path,
    *,
    model: str,
    base_url: str,
    top_k: int = 5,
) -> dict[str, Any]:
    """One frozen Parent localization turn (LLM + deterministic BM25 fallback).

    Returns ``{"subagent_query", "parent_notes", "candidate_files"}``. The candidate files
    come from the mini model when it returns parseable paths, filled (or replaced) by BM25
    over the repo when fewer than ``top_k`` survive.
    """
    problem = instance["problem_statement"]
    files = list_python_files(repo_root)
    listing = "\n".join(files)
    prompt = (
        "You are a senior engineer triaging a GitHub issue for a coding sub-agent. "
        "Return ONLY one JSON object with keys: candidate_files (a list of up to "
        f"{top_k} repository file paths, in rank order, most likely to need editing), "
        "subagent_query (one or two sentences directing the fix), parent_notes (a short "
        "summary of the suspected root cause and a reproduction command).\n\n"
        f"Repository python files:\n{truncate_tokens(listing, 4000)}\n\n"
        f"Issue:\n{problem}\n"
    )
    candidate_files: list[str] = []
    subagent_query = ""
    parent_notes = ""
    try:
        with httpx.Client(timeout=120.0, trust_env=False) as client:
            response = client.post(
                f"{base_url}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": TEMPERATURE,
                    "max_tokens": 512,
                },
            )
            response.raise_for_status()
        parsed = _parse_parent_json(response.json()["choices"][0]["message"]["content"])
        if parsed is not None:
            candidate_files = [
                rel for rel in parsed.get("candidate_files", []) if isinstance(rel, str)
            ]
            subagent_query = (
                parsed["subagent_query"] if isinstance(parsed.get("subagent_query"), str) else ""
            )
            parent_notes = (
                parsed["parent_notes"] if isinstance(parsed.get("parent_notes"), str) else ""
            )
    except (httpx.HTTPError, KeyError, IndexError):
        pass
    # Only keep candidate paths that actually exist; BM25 fills the gap (never leaks gold).
    existing = {f for f in files}
    candidate_files = [rel for rel in candidate_files if rel in existing]
    for rel in _bm25_candidate_files(repo_root, files, problem, top_k):
        if rel not in candidate_files:
            candidate_files.append(rel)
        if len(candidate_files) >= top_k:
            break
    return {
        "subagent_query": subagent_query,
        "parent_notes": parent_notes,
        "candidate_files": candidate_files[:top_k],
    }


def parent_handoff(
    instance: dict[str, Any],
    repo_root: Path,
    *,
    model: str,
    base_url: str,
    cache_dir: Path,
    force: bool = False,
) -> dict[str, Any]:
    """Produce the frozen Parent hand-off once and cache it to disk (JSON)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{instance['instance_id']}.json"
    if path.is_file() and not force:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    result = localize_parent(instance, repo_root, model=model, base_url=base_url)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


# ------------------------------------------------------------------------ sub-agent run


def _sidecar_model_kwargs(base_url: str) -> dict[str, Any]:
    return {
        "api_base": base_url,
        "custom_llm_provider": "openai",
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "drop_params": True,
    }


def run_subagent(
    instance: dict[str, Any],
    handoff_text: str,
    *,
    model: str,
    base_url: str,
    output_path: Path,
) -> dict[str, Any]:
    """Run one mini-swe-agent sub-agent in Docker through ``cfx serve``; return the receipt.

    Returns ``{exit_status, steps, submission}``. A context-window overflow surfaces as
    ``exit_status == "ContextWindowExceededError"`` with a best-effort partial ``git diff``.
    """
    from minisweagent.agents.default import DefaultAgent  # local: WSL-only dependency
    from minisweagent.config import get_config_from_spec
    from minisweagent.environments.docker import DockerEnvironment
    from minisweagent.models.litellm_model import LitellmModel
    from minisweagent.run.benchmarks.swebench import get_swebench_docker_image_name

    cfg = get_config_from_spec("swebench.yaml")
    agent_cfg = dict(cfg["agent"])
    env_cfg = dict(cfg["environment"])
    model_cfg = dict(cfg["model"])
    env_cfg.pop("environment_class", None)

    environment = DockerEnvironment(image=get_swebench_docker_image_name(instance), **env_cfg)
    model_obj = LitellmModel(
        model_name=model,
        model_kwargs=_sidecar_model_kwargs(base_url),
        cost_tracking="ignore_errors",
        observation_template=model_cfg.get("observation_template"),
        format_error_template=model_cfg.get("format_error_template"),
    )
    agent = DefaultAgent(
        model_obj,
        environment,
        system_template=agent_cfg["system_template"],
        instance_template=agent_cfg["instance_template"],
        step_limit=STEP_LIMIT,
        cost_limit=0.0,
        output_path=output_path,
    )
    started = time.perf_counter()
    try:
        info = agent.run(task=handoff_text)
        exit_status = str(info.get("exit_status", ""))
        submission = str(info.get("submission", ""))
    except Exception as exc:  # ContextWindowExceededError and other aborts propagate here
        exit_status = type(exc).__name__
        try:
            out = environment.execute({"command": "git diff"})
            submission = out["output"] if out.get("returncode") == 0 else ""
        except Exception:
            submission = ""
    finally:
        environment.cleanup()
    return {
        "exit_status": exit_status,
        "steps": agent.n_calls,
        "submission": submission,
        "wall_time_s": round(time.perf_counter() - started, 3),
    }


# ----------------------------------------------------------------------------- grading


def grade(
    predictions_path: Path,
    *,
    run_id: str,
    max_workers: int = 2,
) -> dict[str, int]:
    """Grade a per-arm SWE-bench predictions JSONL via the harness Python API (Docker).

    The HF Verified split has no ``image`` column, so each instance is stamped with the
    harness's canonical image name (``sweb.eval.x86_64.<id>``, ``__`` -> ``_1776_``).
    Empty-patch predictions (a sub-agent that overflowed before editing) are not graded and
    resolve to 0.
    """
    from swebench.harness.reporting import make_run_report  # local: WSL-only dependency
    from swebench.harness.run_evaluation import (
        _docker_client,
        get_predictions_from_file,
        run_instances,
    )
    from swebench.harness.utils import load_swebench_dataset
    from swebench.task.checks import expected_image

    predictions = {
        pred["instance_id"]: pred
        for pred in get_predictions_from_file(str(predictions_path), DATASET, "test")
    }
    instance_ids = list(predictions)
    instances = load_swebench_dataset(DATASET, "test", instance_ids)
    for instance in instances:
        instance["image"] = expected_image(instance["instance_id"])
    graded = [
        instance
        for instance in instances
        if predictions[instance["instance_id"]].get("model_patch")
    ]
    if graded:
        run_instances(
            predictions,
            graded,
            max_workers,
            run_id,
            1800,
            rewrite_reports=False,
            task_repo=None,
        )
    report_path = make_run_report(predictions, instances, run_id, _docker_client(), str(REPORTS))
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    resolved: dict[str, int] = {instance_id: 0 for instance_id in instance_ids}
    for instance_id in report.get("resolved_ids", []):
        resolved[instance_id] = 1
    return resolved


# ------------------------------------------------------------------------------- receipt


def _token_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {key: after[key] - before[key] for key in before}


def write_prediction(
    predictions_path: Path, instance_id: str, arm: str, model: str, patch: str
) -> None:
    predictions_path.parent.mkdir(parents=True, exist_ok=True)
    with predictions_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "instance_id": instance_id,
                    "model_name_or_path": model,
                    "model_patch": patch,
                    "arm": arm,
                }
            )
            + "\n"
        )


def project_tokens(triplet_tokens: int, n: int, daily_cap: int) -> dict[str, Any]:
    """Project the full-run token load and the day count at ``daily_cap``."""
    total = triplet_tokens * n
    return {
        "smoke_triplet_tokens": triplet_tokens,
        "projected_total_tokens_30": total,
        "days_at_cap": (total + daily_cap - 1) // daily_cap if daily_cap > 0 else None,
        "daily_cap": daily_cap,
    }


# ---------------------------------------------------------------------------------- main


def _sidecar_command(args: argparse.Namespace, arm: str) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "sidecar",
        "serve",
        "--upstream",
        args.upstream,
        "--mode",
        "passthrough",
        "--port",
        str(args.port),
        "--window-cap",
        str(args.window_cap),
        "--b8-ledger-dir",
        args.ledger_dir,
        "--b8-daily-cap",
        str(args.daily_cap),
    ]
    if arm == "C":
        command += ["--obs-fold", "--obs-store", str(Path(args.ledger_dir) / "obs.db")]
    return command


def _start_sidecar(args: argparse.Namespace, arm: str) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        _sidecar_command(args, arm),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(200):
        if proc.poll() is not None:
            raise RuntimeError("cfx serve exited during startup")
        try:
            httpx.get(f"http://127.0.0.1:{args.port}/fabric/health", timeout=1.0)
            return proc
        except httpx.HTTPError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("cfx serve did not become healthy")


def _stop_sidecar(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.b8_live",
        description="B8 live harness: paired sub-agent arms under a capped window.",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--n", type=int, default=DEFAULT_N)
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="run only the first N sampled tasks (0 = all)",
    )
    parser.add_argument("--model", default=os.environ.get("CF_B8_MODEL", "gpt-4.1-mini"))
    parser.add_argument(
        "--upstream",
        default=os.environ.get("CF_B8_UPSTREAM", "https://api.openai.com"),
        help="OpenAI-compatible upstream the sidecar proxies to",
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("CF_PORT", "8080")))
    parser.add_argument(
        "--no-sidecar",
        action="store_true",
        help="use an externally-run sidecar at --base-url instead of spawning one per arm",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("CF_B8_BASE_URL", "http://127.0.0.1:8080/v1"),
        help="cfx serve base URL (used only with --no-sidecar)",
    )
    parser.add_argument("--repo-dir", default=os.environ.get("CF_B8_REPO_DIR", ""))
    parser.add_argument("--parent-cache", default=str(PARENT_CACHE))
    parser.add_argument("--ledger-dir", default=str(LEDGER_DIR))
    parser.add_argument("--window-cap", type=int, default=WINDOW_CAP)
    parser.add_argument("--daily-cap", type=int, default=b8.DEFAULT_DAILY_CAP)
    parser.add_argument("--out", default="")
    parser.add_argument("--resume", action="store_true", help="skip finished task x arm pairs")
    parser.add_argument("--grade", action="store_true", help="grade predictions after running")
    parser.add_argument("--max-workers", type=int, default=2, help="grading worker count (Docker)")
    return parser.parse_args(argv)


def _require_model(model: str) -> None:
    if not b8.is_allowed_model(model):
        raise SystemExit(
            f"model {model!r} is not in the B8 allow-list {b8.ALLOWED_MODELS}; refusing to run"
        )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _require_model(args.model)
    if not args.repo_dir:
        raise SystemExit("--repo-dir (or CF_B8_REPO_DIR) is required: the extracted repo checkout")

    ledger = b8.Ledger(args.ledger_dir)
    day = b8.utc_day()
    instances = sample_instances(load_instances(), args.seed, args.n)
    selected = instances[: args.limit] if args.limit else instances
    print(f"b8_live: seed={args.seed} n={args.n} selected={len(selected)} model={args.model}")

    predictions_path = REPORTS / "b8_predictions.jsonl"
    done = finished_pairs(predictions_path) if args.resume else set()

    base_url = args.base_url if args.no_sidecar else f"http://127.0.0.1:{args.port}/v1"
    parent_tokens: dict[str, int] = {}
    if not args.no_sidecar:
        # Phase 1: produce each task's frozen Parent hand-off once (one mini-tier turn).
        parent_proc = _start_sidecar(args, "A")
        before = ledger.summary(day)
        for instance in selected:
            repo_root = Path(args.repo_dir) / instance["instance_id"]
            if repo_root.is_dir():
                parent_handoff(
                    instance,
                    repo_root,
                    model=args.model,
                    base_url=base_url,
                    cache_dir=Path(args.parent_cache),
                )
        parent_tokens = _token_delta(before, ledger.summary(day))
        _stop_sidecar(parent_proc)

    arm_receipts: dict[str, dict[str, Any]] = {arm: {} for arm in ARMS}
    for arm in ARMS:
        work = pending_pairs(selected, (arm,), done)
        if not work:
            continue
        proc: subprocess.Popen[bytes] | None = None
        if not args.no_sidecar:
            proc = _start_sidecar(args, arm)
        try:
            for instance, _arm in work:
                instance_id = instance["instance_id"]
                repo_root = Path(args.repo_dir) / instance_id
                if not repo_root.is_dir():
                    print(
                        f"b8_live: repo not found for {instance_id} at {repo_root}; skipping",
                        file=sys.stderr,
                    )
                    continue
                parent = parent_handoff(
                    instance,
                    repo_root,
                    model=args.model,
                    base_url=base_url,
                    cache_dir=Path(args.parent_cache),
                )
                objective = " ".join(
                    part
                    for part in (instance["problem_statement"], parent.get("subagent_query", ""))
                    if part
                )
                handoff = build_handoff(
                    arm,
                    repo_root,
                    parent["candidate_files"],
                    objective,
                    project_id=instance["repo"].split("/")[-1],
                )
                before = ledger.summary(day)
                output_path = Path(args.repo_dir) / instance_id / f"{instance_id}.{arm}.traj.json"
                receipt = run_subagent(
                    instance,
                    handoff,
                    model=args.model,
                    base_url=base_url,
                    output_path=output_path,
                )
                tokens = _token_delta(before, ledger.summary(day))
                write_prediction(
                    predictions_path, instance_id, arm, args.model, receipt["submission"]
                )
                arm_receipts[arm][instance_id] = {
                    "exit_status": receipt["exit_status"],
                    "overflow": receipt["exit_status"] == "ContextWindowExceededError",
                    "steps": receipt["steps"],
                    "wall_time_s": receipt["wall_time_s"],
                    "tokens": tokens,
                }
                print(
                    f"b8_live: {instance_id} arm {arm} -> {receipt['exit_status']} "
                    f"steps={receipt['steps']} tokens={tokens['total_tokens']}"
                )
        finally:
            if proc is not None:
                _stop_sidecar(proc)

    resolved: dict[str, dict[str, int]] = {arm: {} for arm in ARMS}
    if args.grade:
        for arm in ARMS:
            if not arm_receipts[arm]:
                continue
            per_arm_preds = predictions_path.with_suffix(f".{arm}.jsonl")
            with (
                predictions_path.open(encoding="utf-8") as src,
                per_arm_preds.open("w", encoding="utf-8") as dst,
            ):
                for line in src:
                    row = json.loads(line)
                    if row.get("arm") == arm:
                        dst.write(line)
            resolved[arm] = grade(per_arm_preds, run_id=f"b8_{arm}")
            for instance_id, value in resolved[arm].items():
                arm_receipts[arm].setdefault(instance_id, {})["resolved"] = value

    ledger_summary = ledger.summary(day)
    receipt = {
        "scenario": "b8_live",
        "kind": "smoke" if args.limit else "live",
        "seed": args.seed,
        "n": args.n,
        "model": args.model,
        "window_cap": args.window_cap,
        "handoff_budget": HANDOFF_BUDGET,
        "step_limit": STEP_LIMIT,
        "started_at": datetime.now(tz=UTC).isoformat(),
        "day": day,
        "instance_ids": [i["instance_id"] for i in selected],
        "arms": arm_receipts,
        "resolved": resolved,
        "parent_tokens": parent_tokens,
        "ledger": ledger_summary,
        "projection": project_tokens(ledger_summary["total_tokens"], args.n, args.daily_cap),
    }
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) if args.out else REPORTS / f"b8_smoke_{ts}.json"
    out.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    _print_report(receipt, out)
    return 0


def _print_report(receipt: dict[str, Any], out: Path) -> None:
    print(f"\nb8_live report ({out}):")
    for arm in ARMS:
        summary = receipt["arms"].get(arm, {})
        rows = list(summary.values())
        total = sum(r["tokens"]["total_tokens"] for r in rows)
        cached = sum(r["tokens"]["cached_tokens"] for r in rows)
        uncached = sum(r["tokens"]["uncached_tokens"] for r in rows)
        overflows = sum(1 for r in rows if r["overflow"])
        resolved = sum(1 for r in rows if r.get("resolved") == 1)
        steps = [r["steps"] for r in rows]
        wall = [r["wall_time_s"] for r in rows]
        print(
            f"  arm {arm}: tasks={len(rows)} resolved={resolved} overflows={overflows} "
            f"billed={total} cached={cached} uncached={uncached} "
            f"steps={steps} wall_s={wall}"
        )
    ledger = receipt["ledger"]
    proj = receipt["projection"]
    print(
        f"  ledger total (day {receipt['day']}): {ledger['total_tokens']} "
        f"(prompt {ledger['prompt_tokens']}, completion {ledger['completion_tokens']})"
    )
    print(
        f"  projection: {proj['projected_total_tokens_30']} tokens for 30 tasks x 3 arms "
        f"= {proj['days_at_cap']} days at {proj['daily_cap']}/day"
    )


if __name__ == "__main__":
    raise SystemExit(main())
