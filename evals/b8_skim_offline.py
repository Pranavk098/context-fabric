"""Offline skim eval — R vs SKIM vs the old compile packet (dev set, no GPU, no API).

**EXPLORATORY. Not a result of record.** The B4 v3 objectives are a dev set
(``evals/fixtures/b4v3_objectives.json``, frozen sha256); over-fitting here is expected and
a held-out run would be required to claim anything. Receipts land under
``evals/reports/b8_skim_offline_*.json``.

Compares, at 1k/2k/4k token budgets, the prompt-token cost and required-fact recall of
three hand-off packets over the full ``httpx`` package:

* ``R``     — BM25 over AST chunks (the frozen B4 v3 retrieval arm), top chunks to budget.
* ``SKIM``  — ``kernel.router.skim.skim``: question-guided coarse-to-fine selection of real
  source lines, verbatim with ``file:line`` anchors.
* ``C``     — the old compile packet (mechanical fabric + shared Tier-0 cartridge + terse
  answer contract + ownership map), ``budget_base`` = budget.

Recall uses the fixed-scorer token match (leading/trailing ``.``/``-``/``/`` stripped, the
B4 v3 correction). Offline only: ``--dry-run`` builds every packet with no network.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from adapters.langgraph import compile_node_packet, node_model_input
from evals.b4_v3 import (
    _TERSE,
    OWNERSHIP,
    SCOPE,
    AgentSpec,
    _Counter,
    build_ast_chunks,
    build_compile_blocks,
    load_objectives,
    package_files,
    stage_of,
)
from evals.mech_fabric import build_project_cartridge
from evals.rx_hybrid import _fact_present_fixed, _retrieve
from kernel.cartridge.serializer import count_tokens
from kernel.router.compiler import DEFAULT_AS_OF
from kernel.router.skim import skim
from protocol.models import Recipient

REPORTS = Path(__file__).parent / "reports"

ARMS = ("R", "SKIM", "C")
MODEL_ARMS = ("SKIM-PPL", "SKIM-PRUNER")
BUDGETS = (1000, 2000, 4000)
NOTE = (
    "exploratory dev-set recall diagnostic; the frozen result docs are "
    "B4-RESULT.md / B4-V3-RESULT.md / RESEARCH-ROUND-C-INTERNAL.md"
)


def _system(
    arm: str,
    budget: int,
    agent: AgentSpec,
    *,
    fixture: Path,
    files: Sequence[str],
    chunks: Sequence[Any],
    blocks: Sequence[Any],
    cartridge: Any,
    counter: _Counter,
    as_of: datetime,
) -> str:
    if arm == "R":
        selected = _retrieve(agent, chunks, counter, budget)
        return "\n\n".join(c.content or "" for c in selected)
    if arm == "SKIM":
        return skim(agent.objective, files, root=fixture, budget=budget, counter=counter.count)
    if arm == "C":
        result = compile_node_packet(
            {"objective": agent.objective, "blocks": blocks},
            scope=SCOPE,
            recipient=Recipient(
                agent_id=agent.objective_id, role=agent.role, stage=stage_of(agent.role)
            ),
            as_of=as_of,
            budget_base=budget,
            cartridge=cartridge,
            output_contract=_TERSE,
        )
        return node_model_input(result)["content"]
    raise ValueError(f"unknown arm {arm!r}")


def _arm_key(arm: str, budget: int) -> str:
    return f"{arm}:{budget}"


def dry_run_payload(
    fixture: Path, objectives: Sequence[AgentSpec], *, model: str, as_of: datetime = DEFAULT_AS_OF
) -> dict[str, Any]:
    files = package_files(fixture)
    counter = _Counter(model)
    chunks = build_ast_chunks(fixture, files)
    cartridge = build_project_cartridge(fixture, files)
    blocks = build_compile_blocks(fixture, files, OWNERSHIP)

    per_arm: dict[str, dict[str, Any]] = {}
    for budget in BUDGETS:
        for arm in ARMS:
            total = 0
            rows: dict[str, dict[str, Any]] = {}
            for agent in objectives:
                system = _system(
                    arm,
                    budget,
                    agent,
                    fixture=fixture,
                    files=files,
                    chunks=chunks,
                    blocks=blocks,
                    cartridge=cartridge,
                    counter=counter,
                    as_of=as_of,
                )
                tokens = counter.count(system) + counter.count(agent.objective)
                total += tokens
                rows[agent.objective_id] = {"role": agent.role, "prompt_tokens": tokens}
            per_arm[_arm_key(arm, budget)] = {
                "arm": arm,
                "budget": budget,
                "prompt_tokens_total": total,
                "max_objective_prompt_tokens": max(r["prompt_tokens"] for r in rows.values()),
                "objectives": rows,
            }

    recall: list[dict[str, Any]] = []
    for budget in BUDGETS:
        for arm in ARMS:
            for agent in objectives:
                system = _system(
                    arm,
                    budget,
                    agent,
                    fixture=fixture,
                    files=files,
                    chunks=chunks,
                    blocks=blocks,
                    cartridge=cartridge,
                    counter=counter,
                    as_of=as_of,
                )
                facts = {fact: _fact_present_fixed(system, fact) for fact in agent.required}
                recall.append(
                    {
                        "arm": _arm_key(arm, budget),
                        "objective_id": agent.objective_id,
                        "role": agent.role,
                        "required": list(agent.required),
                        "facts_in_packet": facts,
                        "all_in_packet": all(facts.values()),
                    }
                )

    return {
        "scenario": "b8_skim_offline",
        "kind": "dry_run",
        "note": NOTE,
        "model": model,
        "tokenizer": counter.kind,
        "budgets": list(BUDGETS),
        "n_objectives": len(objectives),
        "n_files": len(files),
        "bundle_tokens_tiktoken": count_tokens(
            "\n\n".join(
                (fixture / rel).read_text(encoding="utf-8", errors="replace") for rel in files
            )
        ),
        "arms": per_arm,
        "recall": recall,
    }


def _print_dry_run(receipt: dict[str, Any]) -> None:
    print(
        f"b8_skim_offline: kind=dry_run model={receipt['model']} "
        f"tokenizer={receipt['tokenizer']} n_objectives={receipt['n_objectives']}"
    )
    print(
        f"fixture: {receipt['n_files']} files, {receipt['bundle_tokens_tiktoken']} tiktoken"
        " (full package)"
    )
    print(f"{'arm':<10}{'prompt_tok_total':>17}{'max_obj':>9}{'all_facts':>10}{'fact_cov':>9}")
    # aggregate recall per arm/budget
    by_arm: dict[str, dict[str, Any]] = {}
    for row in receipt["recall"]:
        a = by_arm.setdefault(row["arm"], {"all": 0, "facts": 0, "total": 0})
        a["all"] += 1 if row["all_in_packet"] else 0
        a["facts"] += sum(row["facts_in_packet"].values())
        a["total"] += len(row["required"])
    for key, arm in receipt["arms"].items():
        r = by_arm[key]
        n_obj = receipt["n_objectives"]
        print(
            f"{key:<10}{arm['prompt_tokens_total']:>17}{arm['max_objective_prompt_tokens']:>9}"
            f"{str(r['all']) + '/' + str(n_obj):>10}"
            f"{str(r['facts']) + '/' + str(r['total']):>9}"
        )


def _p95(vals: Sequence[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[max(int(len(s) * 0.95) - 1, 0)]


def model_arms_payload(
    fixture: Path, objectives: Sequence[AgentSpec], *, model: str
) -> dict[str, Any]:
    """SKIM-PPL / SKIM-PRUNER at 1k/2k/4k (local GPU, lazy torch) + recall + latency."""
    from kernel.router.skim_model import ModelSkimmer, ppl_scorer, pruner_scorer

    files = package_files(fixture)
    counter = _Counter(model)
    factories = {"SKIM-PPL": ppl_scorer, "SKIM-PRUNER": pruner_scorer}

    per_arm: dict[str, dict[str, Any]] = {}
    recall: list[dict[str, Any]] = []
    latency: dict[str, dict[str, Any]] = {}
    for arm in MODEL_ARMS:
        sk = ModelSkimmer(fixture, files, scorer=factories[arm](), counter=counter.count)
        cold_ms: list[float] = []
        warm_ms: list[float] = []
        for budget in BUDGETS:
            total = 0
            rows: dict[str, dict[str, Any]] = {}
            for agent in objectives:
                t0 = time.perf_counter()
                system = sk.skim(agent.objective, budget=budget)
                dt = (time.perf_counter() - t0) * 1000.0
                if budget == BUDGETS[0]:
                    cold_ms.append(dt)
                else:
                    warm_ms.append(dt)
                tokens = counter.count(system) + counter.count(agent.objective)
                total += tokens
                rows[agent.objective_id] = {"role": agent.role, "prompt_tokens": tokens}
                facts = {fact: _fact_present_fixed(system, fact) for fact in agent.required}
                recall.append(
                    {
                        "arm": _arm_key(arm, budget),
                        "objective_id": agent.objective_id,
                        "role": agent.role,
                        "required": list(agent.required),
                        "facts_in_packet": facts,
                        "all_in_packet": all(facts.values()),
                    }
                )
            per_arm[_arm_key(arm, budget)] = {
                "arm": arm,
                "budget": budget,
                "prompt_tokens_total": total,
                "max_objective_prompt_tokens": max(r["prompt_tokens"] for r in rows.values()),
                "objectives": rows,
            }
        latency[arm] = {
            "cold_ms_mean": round(sum(cold_ms) / len(cold_ms), 1) if cold_ms else 0.0,
            "cold_ms_p95": round(_p95(cold_ms), 1),
            "warm_ms_mean": round(sum(warm_ms) / len(warm_ms), 2) if warm_ms else 0.0,
        }
    return {
        "scenario": "b8_skim_offline",
        "kind": "model_arms",
        "note": NOTE,
        "model": model,
        "tokenizer": counter.kind,
        "budgets": list(BUDGETS),
        "n_objectives": len(objectives),
        "n_files": len(files),
        "bundle_tokens_tiktoken": count_tokens(
            "\n\n".join(
                (fixture / rel).read_text(encoding="utf-8", errors="replace") for rel in files
            )
        ),
        "arms": per_arm,
        "recall": recall,
        "latency": latency,
    }


def _print_model_arms(receipt: dict[str, Any]) -> None:
    print(
        f"b8_skim_offline: kind=model_arms model={receipt['model']} "
        f"tokenizer={receipt['tokenizer']} n_objectives={receipt['n_objectives']}"
    )
    by_arm: dict[str, dict[str, Any]] = {}
    for row in receipt["recall"]:
        a = by_arm.setdefault(row["arm"], {"all": 0, "facts": 0, "total": 0})
        a["all"] += 1 if row["all_in_packet"] else 0
        a["facts"] += sum(row["facts_in_packet"].values())
        a["total"] += len(row["required"])
    n_obj = receipt["n_objectives"]
    print(
        f"{'arm':<12}{'prompt_tok_total':>17}{'max_obj':>9}{'all_facts':>10}{'fact_cov':>9}"
        f"{'cold_ms':>10}{'warm_ms':>10}"
    )
    for key, arm in receipt["arms"].items():
        r = by_arm[key]
        arm_name = key.split(":")[0]
        lat = receipt["latency"][arm_name]
        print(
            f"{key:<12}{arm['prompt_tokens_total']:>17}{arm['max_objective_prompt_tokens']:>9}"
            f"{str(r['all']) + '/' + str(n_obj):>10}"
            f"{str(r['facts']) + '/' + str(r['total']):>9}"
            f"{lat['cold_ms_mean']:>10.0f}{lat['warm_ms_mean']:>10.2f}"
        )
    for row in receipt["recall"]:
        marks = " ".join(
            f"{fact}={'Y' if hit else 'N'}" for fact, hit in row["facts_in_packet"].items()
        )
        print(f"  {row['arm']:<12} {row['objective_id']:<10} {marks}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.b8_skim_offline",
        description="Offline skim eval: R vs SKIM vs old compile packet (recall diagnostic).",
    )
    parser.add_argument("--fixture", default=os.environ.get("CF_B4_FIXTURE", ""))
    parser.add_argument("--model", default=os.environ.get("CF_MODEL", "Qwen/Qwen2.5-1.5B-Instruct"))
    parser.add_argument("--dry-run", action="store_true", help="build packets offline, no network")
    parser.add_argument(
        "--model-arms",
        action="store_true",
        help="also run SKIM-PPL / SKIM-PRUNER (local GPU, torch; WSL only)",
    )
    parser.add_argument("--out", default="", help="receipt path override")
    args = parser.parse_args(argv)

    if not args.fixture:
        print("b8_skim_offline: --fixture (or CF_B4_FIXTURE) is required", file=sys.stderr)
        return 2
    fixture = Path(args.fixture)
    if not (fixture / "httpx" / "_api.py").is_file():
        print(f"b8_skim_offline: {fixture} does not look like an httpx clone", file=sys.stderr)
        return 2
    if not args.dry_run:
        print("b8_skim_offline: --dry-run is required (offline-only eval)", file=sys.stderr)
        return 2

    objectives = load_objectives()
    ts = time.strftime("%Y%m%d_%H%M%S")
    REPORTS.mkdir(parents=True, exist_ok=True)
    receipt = dry_run_payload(fixture, objectives, model=args.model)
    out = Path(args.out) if args.out else REPORTS / f"b8_skim_offline_{ts}.json"
    out.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    _print_dry_run(receipt)
    print(f"receipt: {out}")

    if args.model_arms:
        mreceipt = model_arms_payload(fixture, objectives, model=args.model)
        mout = Path(args.out) if args.out else REPORTS / f"b8_skim_model_{ts}.json"
        mout.write_text(json.dumps(mreceipt, indent=2), encoding="utf-8")
        _print_model_arms(mreceipt)
        print(f"receipt: {mout}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
