"""B4 exploration harness — ablate compile-arm levers on the B4 v2 dev set.

**Not a result of record.** The B4 v2 objectives are now a dev set (``order-b/b4-explore``):
we tune on them freely, and a later, separately pre-registered B4 v3 on held-out objectives
will confirm. Each lever runs the same 12 objectives × repeats against the same model and
``temperature=0.0``, with a unique arm tag so vLLM's persistent prefix cache cannot carry
between arms. Receipts land under ``evals/reports/b4x_<ts>.json``.

Levers (vs the frozen v2 ``compile`` arm, ``budget_base=4000``, no Tier-0 cartridge):

* ``prefix``  — arm B reference (raw bundle + role instruction), the criterion baseline.
* ``compile`` — the frozen v2 compile arm (reference to ablate against).
* ``l1``      — pin the cartridge hologram as Tier 0 (shared leading bytes across roles).
* ``l2``      — richer fabric: full ``from ... import ...`` statements + full source bodies
  for the top-k objective-relevant symbols, budget-capped.
* ``l3``      — terse answer-format instruction in the packet contract.
* ``l4_1k`` / ``l4_2k`` — packet budget sweep (``compile`` @4000 is the reference).
* ``l5``      — best combination (l1 + l2 + l3).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from adapters.langgraph import compile_node_packet, node_model_input
from evals.b4_divergent import (
    _AGENTS,
    BUNDLE_FILES,
    OWNERSHIP,
    SCOPE,
    AgentSpec,
    _chat,
    _prompt_stats,
    _upstream_url,
    build_mech_blocks,
    render_bundle,
    role_instruction,
    score_quality,
)
from evals.mech_fabric import build_project_cartridge, build_rich_blocks
from kernel.cartridge.serializer import Cartridge
from kernel.router.compiler import DEFAULT_AS_OF
from protocol.models import BlockKind, ContextBlock, Recipient
from sidecar.demo import _Counters, _delta, _scrape_cache, _start_kv_sampler, _totals

REPORTS = Path(__file__).parent / "reports"

_TERSE = (
    "Reply with ONLY the comma-separated answer values, nothing else: no sentences, "
    "no explanation, no markdown, no code."
)

# (arm name, max_tokens). Ordered as run; order matters only for the cold-first-request
# uncached accounting, which is per-arm and isolated by tag regardless.
_ARMS: tuple[tuple[str, int], ...] = (
    ("prefix", 48),
    ("prefix", 96),
    ("compile", 48),
    ("compile", 96),
    ("l1", 48),
    ("l2", 48),
    ("l3", 48),
    ("l3", 96),
    ("l4_1k", 48),
    ("l4_2k", 48),
    ("l5", 96),
)

_BUDGETS = {"l4_1k": 1000, "l4_2k": 2000}


def _tag(name: str, max_tokens: int) -> str:
    return f"B4x::{name}::{max_tokens}::"


def _apply_ownership(blocks: list[ContextBlock]) -> tuple[ContextBlock, ...]:
    """Mirror ``evals.b4_divergent.build_mech_blocks`` ownership (role -> owned file)."""
    owner_by_file = {rel: role for role, rels in OWNERSHIP.items() for rel in rels}
    roles = tuple(OWNERSHIP)
    out: list[ContextBlock] = []
    for block in blocks:
        owner = owner_by_file.get(block.ref_uri) if block.ref_uri is not None else None
        denied = tuple(r for r in roles if r != owner) if owner else ()
        out.append(block.model_copy(update={"denied_roles": denied}))
    return tuple(out)


def _compile_system(
    agent: AgentSpec,
    blocks: tuple[ContextBlock, ...],
    *,
    tag: str,
    as_of: datetime,
    budget_base: int = 4000,
    cartridge: Cartridge | None = None,
    output_contract: str | None = None,
) -> str:
    result = compile_node_packet(
        {"objective": agent.objective, "blocks": blocks},
        scope=SCOPE,
        recipient=Recipient(agent_id=agent.objective_id, role=agent.role, stage=agent.stage),
        as_of=as_of,
        budget_base=budget_base,
        cartridge=cartridge,
        output_contract=output_contract,
    )
    return f"{tag}\n{node_model_input(result)['content']}"


def _messages(
    name: str,
    max_tokens: int,
    agent: AgentSpec,
    *,
    raw_bundle: str,
    fixture: Path,
    blocks: tuple[ContextBlock, ...],
    cartridge: Cartridge,
    as_of: datetime,
) -> list[dict[str, str]]:
    """One agent's model input for one arm (system tag-prefixed; user = the objective)."""
    tag = _tag(name, max_tokens)
    if name == "prefix":
        system = f"{tag}\n{raw_bundle}\n\n{role_instruction(agent.role, agent.stage)}"
    elif name == "compile":
        system = _compile_system(agent, blocks, tag=tag, as_of=as_of)
    elif name == "l1":
        l1_blocks = tuple(b for b in blocks if b.block_kind is not BlockKind.artifact)
        system = _compile_system(agent, l1_blocks, tag=tag, as_of=as_of, cartridge=cartridge)
    elif name == "l2":
        rich = _apply_ownership(build_rich_blocks(fixture, BUNDLE_FILES, agent.objective))
        system = _compile_system(agent, rich, tag=tag, as_of=as_of)
    elif name == "l3":
        system = _compile_system(agent, blocks, tag=tag, as_of=as_of, output_contract=_TERSE)
    elif name in _BUDGETS:
        system = _compile_system(agent, blocks, tag=tag, as_of=as_of, budget_base=_BUDGETS[name])
    elif name == "l5":
        rich = _apply_ownership(build_rich_blocks(fixture, BUNDLE_FILES, agent.objective))
        l5_blocks = tuple(b for b in rich if b.block_kind is not BlockKind.artifact)
        system = _compile_system(
            agent,
            l5_blocks,
            tag=tag,
            as_of=as_of,
            cartridge=cartridge,
            output_contract=_TERSE,
        )
    else:
        raise ValueError(f"unknown arm {name!r}")
    return [{"role": "system", "content": system}, {"role": "user", "content": agent.objective}]


def _build_arms(
    fixture: Path,
    raw_bundle: str,
    *,
    arms: tuple[tuple[str, int], ...] = _ARMS,
    as_of: datetime = DEFAULT_AS_OF,
) -> list[tuple[str, int, list[tuple[AgentSpec, list[dict[str, str]]]]]]:
    blocks = build_mech_blocks(fixture)
    cartridge = build_project_cartridge(fixture, BUNDLE_FILES)
    out: list[tuple[str, int, list[tuple[AgentSpec, list[dict[str, str]]]]]] = []
    for name, max_tokens in arms:
        pairs = [
            (
                agent,
                _messages(
                    name,
                    max_tokens,
                    agent,
                    raw_bundle=raw_bundle,
                    fixture=fixture,
                    blocks=blocks,
                    cartridge=cartridge,
                    as_of=as_of,
                ),
            )
            for agent in _AGENTS
        ]
        out.append((name, max_tokens, pairs))
    return out


def _key(name: str, max_tokens: int) -> str:
    return f"{name}:{max_tokens}"


def _dry_run(fixture: Path) -> dict[str, Any]:
    rendered, files = render_bundle(fixture)
    arms = _build_arms(fixture, rendered)
    per_arm: dict[str, dict[str, Any]] = {}
    for name, max_tokens, pairs in arms:
        total = 0
        rows: dict[str, dict[str, Any]] = {}
        for agent, messages in pairs:
            stats = _prompt_stats(messages)
            total += int(stats["prompt_tokens"])
            rows[agent.objective_id] = {
                "role": agent.role,
                "prompt_tokens": stats["prompt_tokens"],
                "prompt_sha256": stats["prompt_sha256"],
            }
        per_arm[_key(name, max_tokens)] = {
            "name": name,
            "max_tokens": max_tokens,
            "prompt_tokens_total": total,
            "objectives": rows,
        }
    return {
        "scenario": "b4_explore",
        "kind": "dry_run",
        "fixture": {"files": files},
        "arms": per_arm,
    }


def _run(
    client: httpx.Client, root: str, fixture: Path, *, model: str, repeats: int
) -> dict[str, Any]:
    rendered, _files = render_bundle(fixture)
    arms = _build_arms(fixture, rendered)
    metrics_url = root.rstrip("/") + "/metrics"
    records: list[dict[str, Any]] = []
    summary: dict[str, dict[str, Any]] = {}
    cache: dict[str, dict[str, Any]] = {}
    for name, max_tokens, pairs in arms:
        before = _scrape_cache(client, metrics_url)
        stop_sampler = _start_kv_sampler(client, metrics_url)
        started = time.perf_counter()
        arm_records: list[dict[str, Any]] = []
        for agent, messages in pairs:
            for repeat in range(repeats):
                try:
                    completion = _chat(
                        client, root, model=model, messages=messages, max_tokens=max_tokens
                    )
                    passed, missing, wrong = score_quality(completion.text, agent)
                    error = None
                except httpx.HTTPError as exc:
                    completion = None
                    passed, missing, wrong = False, list(agent.required), list(agent.forbidden)
                    error = str(exc)
                arm_records.append(
                    {
                        "arm": _key(name, max_tokens),
                        "max_tokens": max_tokens,
                        "objective_id": agent.objective_id,
                        "agent": agent.role,
                        "repeat": repeat,
                        "passed": passed,
                        "missing": missing,
                        "wrong": wrong,
                        "error": error,
                        "prompt_tokens": completion.prompt_tokens if completion else 0,
                        "completion_tokens": completion.completion_tokens if completion else 0,
                        "latency_ms": completion.latency_ms if completion else 0.0,
                        "response": completion.text if completion else "",
                        "prompt_sha256": completion.prompt_sha256 if completion else "",
                    }
                )
        wall_ms = (time.perf_counter() - started) * 1000
        after = _scrape_cache(client, metrics_url)
        counters = _Counters(
            _delta(before.hits, after.hits), _delta(before.queries, after.queries), stop_sampler()
        )
        records.extend(arm_records)
        total = len(arm_records)
        n_passed = sum(1 for r in arm_records if r["passed"])
        summary[_key(name, max_tokens)] = {
            **_totals(arm_records, wall_ms),
            "name": name,
            "max_tokens": max_tokens,
            "passed": n_passed,
            "total": total,
            "quality": round(n_passed / total, 4),
            "errors": sum(1 for r in arm_records if r["error"]),
        }
        hits = counters.hits
        prompt = int(summary[_key(name, max_tokens)]["prompt_tokens_total"])
        summary[_key(name, max_tokens)]["uncached_tokens"] = (
            max(prompt - int(round(hits)), 0) if hits is not None else None
        )
        cache[_key(name, max_tokens)] = {
            "prefix_cache_hits_delta": hits,
            "prefix_cache_queries_delta": counters.queries,
            "kv_cache_usage_peak": counters.kv,
        }
    return {
        "scenario": "b4_explore",
        "kind": "live",
        "model": model,
        "upstream": root,
        "ts": time.strftime("%Y%m%d_%H%M%S"),
        "repeats": repeats,
        "records": records,
        "arms": summary,
        "cache": cache,
    }


def _print(receipt: dict[str, Any]) -> None:
    print(f"b4_explore: kind={receipt['kind']}")
    for name, arm in receipt["arms"].items():
        if receipt["kind"] == "dry_run":
            print(f"{name:<9} mt={arm['max_tokens']:>2} prompt_tok={arm['prompt_tokens_total']:>6}")
            continue
        uncached = arm.get("uncached_tokens")
        kv = receipt.get("cache", {}).get(name, {}).get("kv_cache_usage_peak")
        print(
            f"{name:<9} mt={arm.get('max_tokens')!s:>2} quality={arm['passed']}/{arm['total']}"
            f"  billed={arm['prompt_tokens_total']:>6} uncached={uncached} kv={kv}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.b4_explore",
        description="B4 exploration harness (dev set, not a result).",
    )
    parser.add_argument("--fixture", default=os.environ.get("CF_B4_FIXTURE", ""))
    parser.add_argument(
        "--upstream",
        default=os.environ.get("CF_UPSTREAM")
        or os.environ.get("CF_BASE_URL")
        or "http://127.0.0.1:8000",
    )
    parser.add_argument("--model", default=os.environ.get("CF_MODEL", "Qwen/Qwen2.5-1.5B-Instruct"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    if not args.fixture:
        print("b4_explore: --fixture (or CF_B4_FIXTURE) is required", file=sys.stderr)
        return 2
    fixture = Path(args.fixture)
    if not (fixture / "httpx" / "_api.py").is_file():
        print(f"b4_explore: {fixture} does not look like an httpx clone", file=sys.stderr)
        return 2

    if args.dry_run:
        receipt = _dry_run(fixture)
    else:
        upstream = _upstream_url(args.upstream)
        with httpx.Client(timeout=900.0, trust_env=False) as client:
            try:
                receipt = _run(client, upstream, fixture, model=args.model, repeats=args.repeats)
            except httpx.HTTPError as exc:
                print(f"b4_explore: request failed: {exc}", file=sys.stderr)
                return 2

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else REPORTS / f"b4x_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    _print(receipt)
    print(f"receipt: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
