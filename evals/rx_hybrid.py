"""Option-A feasibility probe — "retrieve then compile" vs plain retrieval (dev set).

**EXPLORATORY. Not a result of record.** The B4 v3 objectives are now a dev set
(``evals/fixtures/b4v3_objectives.json``, frozen sha256); we are allowed to over-fit here. A
later, separately pre-registered B4 v4 on held-out objectives would be required to claim any
of this. Receipts land under ``evals/reports/rx_hybrid_*.json``.

Question: on a repository that does **not** fit the context window (the full ``httpx/``
package vs ``--max-model-len 16384``), does a hybrid that retrieves full-body AST chunks by
BM25 and then *compiles* them through the existing compiler beat plain BM25 retrieval at
equal budget — or match it at a lower budget?

Three arms x three budgets (1k/2k/4k), all 20 objectives, ``temperature=0.0``:

* ``R``  — BM25 over AST chunks (one per top-level class/function, full source body, plus
  one module-header chunk per file), top chunks until the budget. This is the frozen B4 v3
  ``retrieval`` arm.
* ``H1`` — the same BM25 retrieval, then the retrieved chunks (full bodies) are compiled
  through ``compile_node_packet`` with the shared Tier-0 cartridge (L1) and the terse answer
  contract (L3), ``budget_base`` = budget.
* ``H2`` — the same BM25 retrieval, then assembled **without the compiler**: the repo-map
  header (the shared cartridge hologram) + the exact ``from X import Y`` import lines + the
  retrieved chunks, deduplicated, all capped at the budget.

The hybrid arms are retrieval-personalized per objective; unlike the B4 v3 compile arm they
do **not** apply the file-ownership (role-divergence) map. Each arm+budget carries a unique
leading cache tag so the persistent vLLM prefix cache cannot carry between arms.

Scoring: the frozen token-level scorer (``evals.b4_v3.score_quality``) and the FIXED scorer
— the same tokenization but with leading/trailing ``.``/``-``/``/`` stripped from every
token (the correction in ``docs/engineering/B4-V3-RESULT.md``). Offline (``--dry-run``)
reports per arm/budget prompt tokens + required-fact recall (fixed scorer); ``--live`` runs
R/H1/H2 at 2k/4k on 1.5B and reports quality under both scorers plus billed/uncached/KV.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from adapters.langgraph import compile_node_packet, node_model_input
from evals.b4_divergent import _chat, _upstream_url
from evals.b4_v3 import (
    _TERSE,
    MAX_TOKENS,
    SCOPE,
    AgentSpec,
    _Counter,
    build_ast_chunks,
    load_objectives,
    package_files,
    render_package,
    stage_of,
)
from evals.b4_v3 import (
    score_quality as score_frozen,
)
from evals.mech_fabric import _import_statements, build_project_cartridge
from kernel.cartridge.serializer import Cartridge, count_tokens
from kernel.router.compiler import DEFAULT_AS_OF
from kernel.router.recall import bm25_scores
from protocol.models import ContextBlock, Recipient
from sidecar.demo import _Counters, _delta, _scrape_cache, _start_kv_sampler, _totals

REPORTS = Path(__file__).parent / "reports"
PREREGISTRATION_NOTE = (
    "exploratory dev-set probe; the frozen result docs are B4-RESULT.md / B4-V3-RESULT.md"
)

ARMS = ("R", "H1", "H2")
DRY_RUN_BUDGETS = (1000, 2000, 4000)
LIVE_BUDGETS = (2000, 4000)

# Unique leading cache tag per arm+budget so vLLM prefix caching cannot carry between arms.
_TAG = "Rx::{}::{}::"


# ---- scorer tokenization -------------------------------------------------------------

# The frozen token rule (B4 v3): tokens are runs of [A-Za-z0-9_.-] plus '-' and '/'.
_TOKEN = re.compile(r"[A-Za-z0-9_.\-/]+")


def _tokens_fixed(text: str) -> list[str]:
    """Fixed tokenization: the frozen rule, then leading/trailing ``.``/``-``/``/`` stripped."""
    return [t.strip(".-/") for t in _TOKEN.findall(text.lower()) if t.strip(".-/")]


def _run_present(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    n = len(needle)
    return n > 0 and any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


def _match_longest(
    answer: list[str], items: Sequence[str], tokenize: Callable[[str], list[str]]
) -> tuple[set[int], list[bool]]:
    """Match items against answer tokens, longest-first, each match consuming its tokens."""
    consumed = [False] * len(answer)
    matched: set[int] = set()
    order = sorted(range(len(items)), key=lambda i: (-len(tokenize(items[i])), -len(items[i])))
    for idx in order:
        toks = tokenize(items[idx])
        if not toks:
            continue
        n = len(toks)
        for pos in range(len(answer) - n + 1):
            if any(consumed[pos + j] for j in range(n)):
                continue
            if answer[pos : pos + n] == toks:
                matched.add(idx)
                for j in range(n):
                    consumed[pos + j] = True
                break
    return matched, consumed


def score_quality_fixed(
    text: str, objective: str, required: Sequence[str], forbidden: Sequence[str]
) -> tuple[bool, list[str], list[str]]:
    """Fixed scorer (strips trailing punctuation). Same pass rule as the frozen scorer."""
    answer = _tokens_fixed(text)
    objective_tokens = _tokens_fixed(objective)
    matched_req, consumed = _match_longest(answer, required, _tokens_fixed)
    missing = [required[i] for i in range(len(required)) if i not in matched_req]
    active = [f for f in forbidden if not _run_present(objective_tokens, _tokens_fixed(f))]
    remaining = [t for t, c in zip(answer, consumed, strict=True) if not c]
    matched_forb, _ = _match_longest(remaining, active, _tokens_fixed)
    wrong = [active[i] for i in sorted(matched_forb)]
    return (not missing and not wrong, missing, wrong)


def _fact_present_fixed(text: str, fact: str) -> bool:
    """Recall diagnostic under the fixed tokenization (exact token match)."""
    return _run_present(_tokens_fixed(text), _tokens_fixed(fact))


# ---- retrieval / assembly -----------------------------------------------------------


def _retrieve(
    agent: AgentSpec, chunks: Sequence[ContextBlock], counter: _Counter, budget: int
) -> list[ContextBlock]:
    """BM25 over the objective, top chunks until ``budget`` (the frozen retrieval rule)."""
    scores = bm25_scores(agent.objective, chunks)
    ranked = sorted(chunks, key=lambda c: (-scores.get(c.block_id, 0.0), c.block_id))
    selected: list[ContextBlock] = []
    spent = 0
    for chunk in ranked:
        if scores.get(chunk.block_id, 0.0) <= 0.0:
            continue
        cost = counter.count(chunk.content or "")
        if spent + cost > budget:
            continue
        selected.append(chunk)
        spent += cost
    return selected


def _dedupe(chunks: Sequence[ContextBlock]) -> list[ContextBlock]:
    """Drop exact-duplicate and contained chunks (first/highest-ranked occurrence wins)."""
    out: list[ContextBlock] = []
    for chunk in chunks:
        text = (chunk.content or "").strip()
        if not text:
            continue
        if any(text in (c.content or "") or (c.content or "") in text for c in out):
            continue
        out.append(chunk)
    return out


def _import_lines(fixture: Path, files: Sequence[str]) -> str:
    """Exact ``from X import Y`` statements for ``files`` (the symbol -> module map)."""
    lines: list[str] = []
    for rel in files:
        text = (fixture / rel).read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        stmts = _import_statements(tree)
        if stmts:
            lines.append(f"{rel}: " + " | ".join(stmts))
    return "\n".join(lines)


def _retrieved_files(chunks: Sequence[ContextBlock]) -> list[str]:
    """The distinct source files of the retrieved chunks, in first-appearance order."""
    seen: list[str] = []
    for chunk in chunks:
        if chunk.ref_uri and chunk.ref_uri not in seen:
            seen.append(chunk.ref_uri)
    return seen


def _r_system(
    agent: AgentSpec, chunks: Sequence[ContextBlock], counter: _Counter, budget: int
) -> str:
    selected = _retrieve(agent, chunks, counter, budget)
    return "\n\n".join(c.content or "" for c in selected)


def _h1_system(
    agent: AgentSpec,
    chunks: Sequence[ContextBlock],
    counter: _Counter,
    budget: int,
    cartridge: Cartridge,
    as_of: datetime,
) -> str:
    selected = _retrieve(agent, chunks, counter, budget)
    result = compile_node_packet(
        {"objective": agent.objective, "blocks": tuple(selected)},
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


def _h2_system(
    agent: AgentSpec,
    chunks: Sequence[ContextBlock],
    counter: _Counter,
    budget: int,
    cartridge: Cartridge,
    fixture: Path,
) -> str:
    selected = _dedupe(_retrieve(agent, chunks, counter, budget))
    imports = _import_lines(fixture, _retrieved_files(selected))
    header = cartridge.hologram_markdown
    parts = [header]
    spent = counter.count(header)
    if imports:
        icost = counter.count(imports)
        if spent + icost <= budget:
            parts.append(imports)
            spent += icost
    body: list[str] = []
    for chunk in selected:
        cost = counter.count(chunk.content or "")
        if spent + cost > budget:
            continue
        body.append(chunk.content or "")
        spent += cost
    if body:
        parts.append("\n\n".join(body))
    return "\n\n".join(parts)


def _arm_tag(arm: str, budget: int) -> str:
    return _TAG.format(arm, budget)


def _system(
    arm: str,
    budget: int,
    agent: AgentSpec,
    *,
    fixture: Path,
    files: Sequence[str],
    chunks: Sequence[ContextBlock],
    cartridge: Cartridge,
    counter: _Counter,
    as_of: datetime,
) -> str:
    tag = _arm_tag(arm, budget)
    if arm == "R":
        body = _r_system(agent, chunks, counter, budget)
    elif arm == "H1":
        body = _h1_system(agent, chunks, counter, budget, cartridge, as_of)
    elif arm == "H2":
        body = _h2_system(agent, chunks, counter, budget, cartridge, fixture)
    else:
        raise ValueError(f"unknown arm {arm!r}")
    return f"{tag}\n{body}"


def _messages(system: str, agent: AgentSpec) -> list[dict[str, str]]:
    return [{"role": "system", "content": system}, {"role": "user", "content": agent.objective}]


def build_arm_messages(
    arm: str,
    budget: int,
    objectives: Sequence[AgentSpec],
    *,
    fixture: Path,
    files: Sequence[str],
    chunks: Sequence[ContextBlock],
    cartridge: Cartridge,
    counter: _Counter,
    as_of: datetime,
) -> list[tuple[AgentSpec, list[dict[str, str]]]]:
    return [
        (
            agent,
            _messages(
                _system(
                    arm,
                    budget,
                    agent,
                    fixture=fixture,
                    files=files,
                    chunks=chunks,
                    cartridge=cartridge,
                    counter=counter,
                    as_of=as_of,
                ),
                agent,
            ),
        )
        for agent in objectives
    ]


def _arm_key(arm: str, budget: int) -> str:
    return f"{arm}:{budget}"


# ---- dry run -------------------------------------------------------------------------


def dry_run_payload(
    fixture: Path, objectives: Sequence[AgentSpec], *, model: str, as_of: datetime = DEFAULT_AS_OF
) -> dict[str, Any]:
    files = package_files(fixture)
    counter = _Counter(model)
    chunks = build_ast_chunks(fixture, files)
    cartridge = build_project_cartridge(fixture, files)
    per_arm: dict[str, dict[str, Any]] = {}
    for budget in DRY_RUN_BUDGETS:
        for arm in ARMS:
            pairs = build_arm_messages(
                arm,
                budget,
                objectives,
                fixture=fixture,
                files=files,
                chunks=chunks,
                cartridge=cartridge,
                counter=counter,
                as_of=as_of,
            )
            total = 0
            rows: dict[str, dict[str, Any]] = {}
            for agent, messages in pairs:
                tokens = sum(counter.count(m["content"]) for m in messages)
                total += tokens
                rows[agent.objective_id] = {"role": agent.role, "prompt_tokens": tokens}
            per_arm[_arm_key(arm, budget)] = {
                "arm": arm,
                "budget": budget,
                "prompt_tokens_total": total,
                "max_objective_prompt_tokens": max(r["prompt_tokens"] for r in rows.values()),
                "objectives": rows,
            }
    # Recall: required facts present in the delivered system content, fixed tokenization.
    recall: list[dict[str, Any]] = []
    for budget in DRY_RUN_BUDGETS:
        for arm in ARMS:
            pairs = build_arm_messages(
                arm,
                budget,
                objectives,
                fixture=fixture,
                files=files,
                chunks=chunks,
                cartridge=cartridge,
                counter=counter,
                as_of=as_of,
            )
            for agent, messages in pairs:
                rendered = messages[0]["content"]
                facts = {fact: _fact_present_fixed(rendered, fact) for fact in agent.required}
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
        "scenario": "rx_hybrid",
        "kind": "dry_run",
        "note": PREREGISTRATION_NOTE,
        "model": model,
        "tokenizer": counter.kind,
        "budgets": list(DRY_RUN_BUDGETS),
        "n_objectives": len(objectives),
        "n_files": len(files),
        "bundle_tokens_tiktoken": count_tokens(render_package(fixture, files)[0]),
        "cartridge_tokens_tiktoken": count_tokens(cartridge.hologram_markdown),
        "arms": per_arm,
        "recall": recall,
    }


# ---- live run ------------------------------------------------------------------------


def _live_configs(budgets: Sequence[int]) -> list[tuple[str, int]]:
    return [(arm, budget) for budget in budgets for arm in ARMS]


def run(
    client: httpx.Client,
    root: str,
    *,
    model: str,
    fixture: Path,
    budgets: Sequence[int],
    ts: str,
) -> dict[str, Any]:
    started_at = datetime.now(tz=UTC).isoformat()
    objectives = load_objectives()
    files = package_files(fixture)
    counter = _Counter(model)
    chunks = build_ast_chunks(fixture, files)
    cartridge = build_project_cartridge(fixture, files)

    metrics_url = root.rstrip("/") + "/metrics"
    records: list[dict[str, Any]] = []
    arms_summary: dict[str, dict[str, Any]] = {}
    per_arm_cache: dict[str, _Counters] = {}
    for arm, budget in _live_configs(budgets):
        key = _arm_key(arm, budget)
        pairs = build_arm_messages(
            arm,
            budget,
            objectives,
            fixture=fixture,
            files=files,
            chunks=chunks,
            cartridge=cartridge,
            counter=counter,
            as_of=DEFAULT_AS_OF,
        )
        before = _scrape_cache(client, metrics_url)
        stop_sampler = _start_kv_sampler(client, metrics_url)
        started = time.perf_counter()
        arm_records: list[dict[str, Any]] = []
        for agent, messages in pairs:
            try:
                completion = _chat(
                    client, root, model=model, messages=messages, max_tokens=MAX_TOKENS
                )
                p_frozen, m_frozen, w_frozen = score_frozen(
                    completion.text, agent.objective, agent.required, agent.forbidden
                )
                p_fixed, m_fixed, w_fixed = score_quality_fixed(
                    completion.text, agent.objective, agent.required, agent.forbidden
                )
                error = None
            except httpx.HTTPError as exc:
                completion = None
                p_frozen = p_fixed = False
                m_fixed = list(agent.required)
                w_fixed = list(agent.forbidden)
                error = str(exc)
            arm_records.append(
                {
                    "op": "chat",
                    "arm": key,
                    "objective_id": agent.objective_id,
                    "agent": agent.role,
                    "objective": agent.objective,
                    "required": list(agent.required),
                    "forbidden": list(agent.forbidden),
                    "passed_frozen": p_frozen,
                    "passed_fixed": p_fixed,
                    "missing_fixed": m_fixed,
                    "wrong_fixed": w_fixed,
                    "error": error,
                    "prompt_tokens": completion.prompt_tokens if completion else 0,
                    "completion_tokens": completion.completion_tokens if completion else 0,
                    "latency_ms": completion.latency_ms if completion else 0.0,
                    "response": completion.text if completion else "",
                    "response_text": (completion.text if completion else "")[:500],
                }
            )
        wall_ms = (time.perf_counter() - started) * 1000
        after = _scrape_cache(client, metrics_url)
        per_arm_cache[key] = _Counters(
            _delta(before.hits, after.hits), _delta(before.queries, after.queries), stop_sampler()
        )
        records.extend(arm_records)
        arms_summary[key] = {
            **_totals(arm_records, wall_ms),
            "arm": arm,
            "budget": budget,
            "passed_frozen": sum(1 for r in arm_records if r["passed_frozen"]),
            "passed_fixed": sum(1 for r in arm_records if r["passed_fixed"]),
            "total": len(arm_records),
            "errors": sum(1 for r in arm_records if r["error"]),
            "quality_frozen": round(
                sum(1 for r in arm_records if r["passed_frozen"]) / len(arm_records), 4
            ),
            "quality_fixed": round(
                sum(1 for r in arm_records if r["passed_fixed"]) / len(arm_records), 4
            ),
        }
        hits = per_arm_cache[key].hits
        arms_summary[key]["uncached_tokens"] = (
            max(int(arms_summary[key]["prompt_tokens_total"]) - int(round(hits)), 0)
            if hits is not None
            else None
        )

    return {
        "scenario": "rx_hybrid",
        "kind": "live",
        "note": PREREGISTRATION_NOTE,
        "model": model,
        "upstream": root,
        "started_at": started_at,
        "ts": ts,
        "tokenizer": counter.kind,
        "budgets": list(budgets),
        "n_objectives": len(objectives),
        "records": records,
        "arms": arms_summary,
        "cache": {
            "available": any(v is not None for c in per_arm_cache.values() for v in c),
            "source": "upstream Prometheus /metrics (vLLM)",
            "per_arm": {
                name: dict(
                    zip(
                        (
                            "prefix_cache_hits_delta",
                            "prefix_cache_queries_delta",
                            "kv_cache_usage_peak",
                        ),
                        counters,
                        strict=True,
                    )
                )
                for name, counters in per_arm_cache.items()
            },
        },
    }


# ---- printing / CLI ------------------------------------------------------------------


def _print_dry_run(receipt: dict[str, Any]) -> None:
    print(
        f"rx_hybrid: kind=dry_run model={receipt['model']} tokenizer={receipt['tokenizer']} "
        f"n_objectives={receipt['n_objectives']}"
    )
    print(
        f"fixture: {receipt['n_files']} files, {receipt['bundle_tokens_tiktoken']} tiktoken "
        f"(full package, > max_model_len 16384)"
    )
    print(f"cartridge (repo map) = {receipt['cartridge_tokens_tiktoken']} tok")
    print(f"{'arm':<10}{'prompt_tok_total':>17}{'max_obj':>9}")
    for key, arm in receipt["arms"].items():
        print(f"{key:<10}{arm['prompt_tokens_total']:>17}{arm['max_objective_prompt_tokens']:>9}")
    print("recall (required facts present in the packet, fixed scorer — OUTCOME):")
    for row in receipt["recall"]:
        marks = " ".join(
            f"{fact}={'Y' if hit else 'N'}" for fact, hit in row["facts_in_packet"].items()
        )
        print(f"  {row['arm']:<10} {row['objective_id']:<10} {marks}")


def _print_receipt(receipt: dict[str, Any]) -> None:
    if receipt["kind"] == "dry_run":
        _print_dry_run(receipt)
        return
    print(f"rx_hybrid: kind=live model={receipt['model']} upstream={receipt['upstream']}")
    print(f"{'arm':<9}{'reqs':>5}{'prompt_tok':>11}{'uncached':>9}{'pass_frz':>9}{'pass_fix':>9}")
    for name, arm in receipt["arms"].items():
        print(
            f"{name:<9}{arm['requests']:>5}{arm['prompt_tokens_total']:>11}"
            f"{str(arm.get('uncached_tokens')):>9}{arm['passed_frozen']:>9}{arm['passed_fixed']:>9}"
        )
    if receipt["cache"]["available"]:
        for name, stats in receipt["cache"]["per_arm"].items():
            print(
                f"cache[{name}]: hits+={stats['prefix_cache_hits_delta']} "
                f"queries+={stats['prefix_cache_queries_delta']} "
                f"kv_peak={stats['kv_cache_usage_peak']}"
            )
    else:
        print("cache: unavailable (upstream has no /metrics)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.rx_hybrid",
        description="Option-A feasibility probe: retrieve-then-compile vs plain retrieval.",
    )
    parser.add_argument("--fixture", default=os.environ.get("CF_B4_FIXTURE", ""))
    parser.add_argument(
        "--upstream",
        default=os.environ.get("CF_UPSTREAM")
        or os.environ.get("CF_BASE_URL")
        or "http://127.0.0.1:8000",
    )
    parser.add_argument("--model", default=os.environ.get("CF_MODEL", "Qwen/Qwen2.5-1.5B-Instruct"))
    parser.add_argument("--dry-run", action="store_true", help="build prompts offline, no network")
    parser.add_argument("--live", action="store_true", help="run against the upstream")
    parser.add_argument("--budgets", default="2000,4000", help="comma-separated budgets for --live")
    parser.add_argument("--out", default="", help="receipt path override")
    args = parser.parse_args(argv)

    if not args.fixture:
        print("rx_hybrid: --fixture (or CF_B4_FIXTURE) is required", file=sys.stderr)
        return 2
    fixture = Path(args.fixture)
    if not (fixture / "httpx" / "_api.py").is_file():
        print(f"rx_hybrid: {fixture} does not look like an httpx clone", file=sys.stderr)
        return 2

    ts = time.strftime("%Y%m%d_%H%M%S")
    if args.dry_run:
        objectives = load_objectives()
        receipt = dry_run_payload(fixture, objectives, model=args.model)
    else:
        budgets = tuple(int(b) for b in args.budgets.split(",") if b.strip())
        if not budgets:
            print("rx_hybrid: --budgets must list at least one positive integer", file=sys.stderr)
            return 2
        upstream = _upstream_url(args.upstream)
        with httpx.Client(timeout=900.0, trust_env=False) as client:
            try:
                receipt = run(
                    client, upstream, model=args.model, fixture=fixture, budgets=budgets, ts=ts
                )
            except httpx.HTTPError as exc:
                print(f"rx_hybrid: request failed: {exc}", file=sys.stderr)
                return 2

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else REPORTS / f"rx_hybrid_{ts}.json"
    out.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    _print_receipt(receipt)
    print(f"receipt: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
