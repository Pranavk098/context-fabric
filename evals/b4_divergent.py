"""Pre-registered B4 harness — divergent-role compiled context vs prefix-cache-only.

Design, roles, objectives, scorer, arms and success criteria are frozen in
``docs/engineering/B4-PREREGISTRATION.md`` (2026-10-18) before any run. One fixture (the
``encode/httpx`` clone at the pinned commit), one 4-file raw bundle (arms A/B's shared
system content, sized < ``--max-model-len 8192``), four role fact blocks (arm C's fabric),
one required+forbidden scorer for every arm, one GPU session.

Arms (all four roles x ``--repeats`` requests, in a fixed order):

* ``broadcast`` — full raw bundle to every agent (no role identity).
* ``prefix``   — shared raw bundle + role instruction appended (the strongest naive
  baseline: identical leading bytes so vLLM prefix caching gets its best case).
* ``compile``  — per-role compiled packet via the LangGraph adapter
  (``compile_node_packet`` + ``node_model_input``).

Each arm's system content carries a unique leading tag so the persistent vLLM prefix cache
cannot carry between arms. Run offline (``--dry-run``, no network) to build every arm's
prompts and print per-arm prompt token counts + a sha256 of each prompt; ``--live`` sends
them to a real OpenAI-compatible upstream and applies the frozen criteria.

Artifact: ``evals/reports/b4_divergent_<ts>.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from adapters.langgraph import compile_node_packet, node_model_input
from kernel.cartridge.serializer import count_tokens
from kernel.router.compiler import DEFAULT_AS_OF
from protocol.canonical import content_hash, sha256_hex
from protocol.models import (
    BlockKind,
    ContextBlock,
    Creator,
    CreatorType,
    Fidelity,
    MemoryTier,
    Recipient,
    Scope,
)
from sidecar.demo import (
    _Completion,
    _Counters,
    _delta,
    _scrape_cache,
    _start_kv_sampler,
    _totals,
)

REPORTS = Path(__file__).parent / "reports"
PREGISTERED_COMMIT = "b5addb64f0161ff6bfe94c124ef76f6a1fba5254"
PREGISTERED_REPO = "https://github.com/encode/httpx"
PREREGISTRATION = "docs/engineering/B4-PREREGISTRATION.md"
MAX_MODEL_LEN = 8192

SCOPE = Scope(tenant_id="default", project_id="httpx-b4", workflow_id="divergent", task_id="t1")

# The fixture bundle: an explicit 4-file slice (preregistration section 2), sized so every
# raw arm fits --max-model-len 8192 (largest measured 6,220 tokens).
BUNDLE_FILES: tuple[str, ...] = (
    "httpx/__init__.py",
    "httpx/__version__.py",
    "httpx/_api.py",
    "httpx/_auth.py",
)

_RENDERING = (
    "files in listed order, raw bytes, joined by a blank line, each prefixed by "
    "'===== FILE: <path> ====='"
)

_ARM_TAGS = {
    "broadcast": "B4::broadcast::",
    "prefix": "B4::prefix::",
    "compile": "B4::compile::",
}

_ROLE_TASKS = {
    "planner": "Plan by locating the relevant module from the context above.",
    "implementer": "Implement by naming the exact function from the context above.",
    "tester": "Test by reading the exact value from the context above.",
    "reviewer": "Review by naming the exact class from the context above.",
}


class AgentSpec(NamedTuple):
    role: str
    stage: str
    objective: str
    required: tuple[str, ...]
    forbidden: tuple[str, ...]


# role, objective, required facts (all), forbidden wrong facts (none) — preregistration
# section 3 (frozen). Required facts are answerable from the raw bundle AND the role's fact
# block; forbidden facts never appear in any fact block or compiled packet.
_AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        "planner",
        "plan",
        "Which module does `httpx/__init__.py` re-export that provides the synchronous "
        "`Client` and asynchronous `AsyncClient` classes? Give the `_X` module name only.",
        ("_client",),
        ("_models",),
    ),
    AgentSpec(
        "implementer",
        "implement",
        "In httpx, which function do the top-level helpers `get()` and `post()` delegate to? "
        "Give the function name only.",
        ("request",),
        ("stream",),
    ),
    AgentSpec(
        "tester",
        "test",
        "What is the exact value of `__version__` in `httpx/__version__.py`?",
        ("0.28.1",),
        ("0.27", "0.28.0", "0.29"),
    ),
    AgentSpec(
        "reviewer",
        "review",
        "Which auth class in `httpx/_auth.py` base64-encodes `username:password` and prefixes "
        "it with `Basic `? Give the class name only.",
        ("basicauth",),
        ("digestauth", "netrcauth", "functionauth"),
    ),
)

AGENT_BY_ROLE = {agent.role: agent for agent in _AGENTS}

# (block_id, content, denied_roles) — one fact block per role, in _AGENTS order
# (preregistration section 4). Each block is denied to the three roles that must not see it.
_FACTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "blk_init",
        "httpx/__init__.py re-exports the package public API. It imports from ._api "
        "import *, from ._auth import *, from ._client import *, from ._config import *, "
        "from ._content import *, from ._exceptions import *, and from ._main import main. "
        "The synchronous Client and asynchronous AsyncClient classes come from ._client.",
        ("implementer", "tester", "reviewer"),
    ),
    (
        "blk_api",
        "httpx/_api.py defines request() and the verb helpers get(), options(), head(), "
        "post(), put(), patch(), delete(). Each verb helper delegates to request() with its "
        "HTTP method, e.g. get() returns request('GET', url, ...) and post() returns "
        "request('POST', url, ...). request() defaults: follow_redirects=False, "
        "trust_env=True, timeout=DEFAULT_TIMEOUT_CONFIG.",
        ("planner", "tester", "reviewer"),
    ),
    (
        "blk_version",
        "httpx/__version__.py defines the package metadata: __title__ = 'httpx', "
        "__description__ = 'A next generation HTTP client, for Python 3.', "
        "__version__ = '0.28.1'.",
        ("planner", "implementer", "reviewer"),
    ),
    (
        "blk_auth",
        "httpx/_auth.py defines the authentication classes. BasicAuth takes a "
        "(username, password) pair and its _build_auth_header base64-encodes "
        "'username:password' and prefixes the result with 'Basic '; the header sets the "
        "Authorization header.",
        ("planner", "implementer", "tester"),
    ),
)


def fact_blocks() -> tuple[ContextBlock, ...]:
    """The four role fact blocks, in _AGENTS order, denied to the three other roles."""
    as_of = DEFAULT_AS_OF
    blocks: list[ContextBlock] = []
    for seq, (block_id, content, denied) in enumerate(_FACTS, start=1):
        blocks.append(
            ContextBlock(
                block_id=block_id,
                block_kind=BlockKind.fact,
                memory_tier=MemoryTier.ledger,
                content=content,
                content_hash="sha256:" + sha256_hex(content.encode("utf-8")),
                token_estimate=count_tokens(content),
                source_event_seq=seq,
                creator=Creator(type=CreatorType.tool, id="b4-fixture"),
                fidelity=Fidelity.extract,
                trust_domain=SCOPE.tenant_id,
                denied_roles=denied,
                observed_at=as_of,
                valid_from=as_of,
            )
        )
    return tuple(blocks)


def render_bundle(root: Path) -> tuple[str, list[dict[str, Any]]]:
    """The raw arm's shared system content: actual bytes, stable order, FILE headers."""
    parts: list[str] = []
    files: list[dict[str, Any]] = []
    for rel in BUNDLE_FILES:
        data = (root / rel).read_bytes()
        text = data.decode("utf-8")
        parts.append(f"===== FILE: {rel} =====\n{text}")
        files.append(
            {
                "path": rel,
                "sha256": "sha256:" + sha256_hex(data),
                "bytes": len(data),
                "tiktoken": count_tokens(text),
            }
        )
    return "\n\n".join(parts), files


def score_quality(text: str, agent: AgentSpec) -> tuple[bool, list[str], list[str]]:
    """The frozen quality gate: all required facts present, no forbidden fact present."""
    lowered = text.lower()
    missing = [fact for fact in agent.required if fact.lower() not in lowered]
    wrong = [fact for fact in agent.forbidden if fact.lower() in lowered]
    return (not missing and not wrong, missing, wrong)


def verify_keys(rendered: str) -> None:
    """Construction checks 1-2 (preregistration section 3), aborting before any request."""
    bundle = rendered.lower()
    problems: list[str] = []
    for agent, (block_id, content, _denied) in zip(_AGENTS, _FACTS, strict=True):
        fact = content.lower()
        for key in agent.required:
            if key.lower() not in bundle:
                problems.append(f"required {key!r} missing from raw bundle")
            if key.lower() not in fact:
                problems.append(f"required {key!r} missing from fact block {block_id}")
        for key in agent.forbidden:
            if key.lower() in fact:
                problems.append(f"forbidden {key!r} present in fact block {block_id}")
    if problems:
        raise ValueError(
            "fixture/content error, run aborted before any request: " + "; ".join(problems)
        )


def role_instruction(role: str, stage: str) -> str:
    return f"ROLE: {role}\nSTAGE: {stage}\n{_ROLE_TASKS[role]}"


def node_messages(
    arm: str,
    agent: AgentSpec,
    *,
    raw_bundle: str,
    blocks: tuple[ContextBlock, ...],
    as_of: datetime,
) -> list[dict[str, str]]:
    """One agent's model input under an arm; the system content is tag-prefixed."""
    tag = _ARM_TAGS[arm]
    if arm == "broadcast":
        system = f"{tag}\n{raw_bundle}"
    elif arm == "prefix":
        system = f"{tag}\n{raw_bundle}\n\n{role_instruction(agent.role, agent.stage)}"
    elif arm == "compile":
        result = compile_node_packet(
            {"objective": agent.objective, "blocks": blocks},
            scope=SCOPE,
            recipient=Recipient(agent_id=agent.role, role=agent.role, stage=agent.stage),
            as_of=as_of,
        )
        system = f"{tag}\n{node_model_input(result)['content']}"
    else:
        raise ValueError(f"unknown arm {arm!r}")
    return [{"role": "system", "content": system}, {"role": "user", "content": agent.objective}]


def build_arms(
    raw_bundle: str,
    blocks: tuple[ContextBlock, ...],
    *,
    as_of: datetime = DEFAULT_AS_OF,
) -> dict[str, list[tuple[str, list[dict[str, str]]]]]:
    """Every arm's prompts, in fixed role order; (role, messages) per request."""
    return {
        arm: [
            (
                agent.role,
                node_messages(arm, agent, raw_bundle=raw_bundle, blocks=blocks, as_of=as_of),
            )
            for agent in _AGENTS
        ]
        for arm in ("broadcast", "prefix", "compile")
    }


def verify_packets(arms: dict[str, list[tuple[str, list[dict[str, str]]]]]) -> None:
    """Construction check 3: each compiled packet carries its required, not forbidden, fact."""
    problems: list[str] = []
    for role, messages in arms["compile"]:
        agent = AGENT_BY_ROLE[role]
        rendered = messages[0]["content"].lower()
        for key in agent.required:
            if key.lower() not in rendered:
                problems.append(f"required {key!r} missing from {role} compiled packet")
        for key in agent.forbidden:
            if key.lower() in rendered:
                problems.append(f"forbidden {key!r} present in {role} compiled packet")
    if problems:
        raise ValueError(
            "compiled-packet content error, run aborted before any request: " + "; ".join(problems)
        )


def commit_of(root: Path) -> tuple[str, str]:
    """(commit, source): git if the clone is readable, else the preregistered hash."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return PREGISTERED_COMMIT, "preregistered"
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip(), "git rev-parse HEAD"
    return PREGISTERED_COMMIT, "preregistered"


def _prompt_stats(messages: list[dict[str, str]]) -> dict[str, Any]:
    prompt_tokens = sum(count_tokens(message["content"]) for message in messages)
    return {
        "prompt_tokens": prompt_tokens,
        "prompt_sha256": "sha256:" + content_hash(messages),
        "system_sha256": "sha256:" + sha256_hex(messages[0]["content"].encode("utf-8")),
        "messages": messages,
    }


def dry_run_payload(
    raw_bundle: str,
    blocks: tuple[ContextBlock, ...],
    *,
    fixture: dict[str, Any],
    as_of: datetime = DEFAULT_AS_OF,
) -> dict[str, Any]:
    """Deterministic offline build of every arm's prompts (no wall clock, no network)."""
    verify_keys(raw_bundle)
    arms = build_arms(raw_bundle, blocks, as_of=as_of)
    verify_packets(arms)
    per_arm: dict[str, dict[str, Any]] = {}
    for arm, pairs in arms.items():
        roles: dict[str, dict[str, Any]] = {}
        total = 0
        for role, messages in pairs:
            stats = _prompt_stats(messages)
            total += int(stats["prompt_tokens"])
            roles[role] = {
                "objective": AGENT_BY_ROLE[role].objective,
                **{k: v for k, v in stats.items() if k != "messages"},
                "messages": stats["messages"],
            }
        per_arm[arm] = {
            "prompt_tokens_total": total,
            "max_role_prompt_tokens": max(r["prompt_tokens"] for r in roles.values()),
            "fits_max_model_len": all(r["prompt_tokens"] < MAX_MODEL_LEN for r in roles.values()),
            "roles": roles,
        }
    return {
        "scenario": "b4_divergent",
        "kind": "dry_run",
        "preregistration": PREREGISTRATION,
        "fixture": fixture,
        "max_model_len": MAX_MODEL_LEN,
        "arms": per_arm,
    }


def _chat(
    client: httpx.Client,
    root: str,
    *,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
) -> _Completion:
    started = time.perf_counter()
    response = client.post(
        f"{root}/v1/chat/completions",
        json={
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "stream": False,
        },
    )
    latency_ms = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    data = response.json()
    usage = data.get("usage") or {}
    message = (data.get("choices") or [{}])[0].get("message") or {}
    return _Completion(
        str(message.get("content") or ""),
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
        round(latency_ms, 1),
        False,
        "sha256:" + content_hash(messages),
    )


def _uncached(prompt_tokens_total: int, hits: float | None) -> int | None:
    """Uncached (real prefill) prompt tokens = billed − prefix-cache hit tokens."""
    if hits is None:
        return None
    return max(prompt_tokens_total - int(round(hits)), 0)


def _criteria(
    arms: dict[str, dict[str, Any]],
    per_arm_cache: dict[str, _Counters],
    records: Sequence[dict[str, Any]],
    repeats: int,
) -> dict[str, Any]:
    b = arms["prefix"]
    c = arms["compile"]
    b_passes = int(b["passed"])
    c_passes = int(c["passed"])
    b_uncached = _uncached(int(b["prompt_tokens_total"]), per_arm_cache["prefix"].hits)
    c_uncached = _uncached(int(c["prompt_tokens_total"]), per_arm_cache["compile"].hits)

    def error_rate(arm: str) -> float:
        total = int(arms[arm]["total"])
        errors = sum(1 for r in records if r["arm"] == arm and r["error"])
        return errors / total if total else 0.0

    token_criterion: dict[str, Any]
    if b_uncached is None or c_uncached is None:
        token_criterion = {
            "pass": None,
            "requirement": "compile uncached tokens <= prefix uncached tokens (factor 1.0x)",
            "note": "cache metrics unavailable; token criterion unevaluable",
        }
    else:
        token_criterion = {
            "pass": c_uncached <= b_uncached,
            "requirement": "compile uncached tokens <= prefix uncached tokens (factor 1.0x)",
            "prefix_uncached_tokens": b_uncached,
            "compile_uncached_tokens": c_uncached,
        }
    quality_ok = c_passes >= b_passes - 1
    unreliable = error_rate("prefix") > 0.25 or error_rate("compile") > 0.25
    token_pass = token_criterion.get("pass")
    if unreliable:
        verdict = "INCONCLUSIVE"
    elif c_passes < b_passes - 1:
        verdict = "KILL"
    elif quality_ok and token_pass is True:
        verdict = "PASS"
    elif quality_ok and token_pass is False:
        verdict = "NO_TOKEN_WIN"
    else:
        verdict = "INCONCLUSIVE"
    return {
        "verdict": verdict,
        "baseline": "prefix",
        "n_roles": len(_AGENTS),
        "repeats": repeats,
        "tolerance_requests": 1,
        "quality": {
            "pass": quality_ok,
            "requirement": "compile passes >= prefix passes - 1",
            "prefix_passes": b_passes,
            "compile_passes": c_passes,
            "total": int(b["total"]),
        },
        "uncached_tokens": token_criterion,
        "unreliable_error_rate": unreliable,
    }


def run(
    client: httpx.Client,
    root: str,
    *,
    model: str,
    fixture: Path,
    repeats: int,
    max_tokens: int,
    ts: str,
) -> dict[str, Any]:
    started_at = datetime.now(tz=UTC).isoformat()
    rendered, files = render_bundle(fixture)
    bundle_tokens = count_tokens(rendered)
    blocks = fact_blocks()
    verify_keys(rendered)
    commit, commit_source = commit_of(fixture)
    arms = build_arms(rendered, blocks)
    verify_packets(arms)

    fixture_manifest = {
        "repo": PREGISTERED_REPO,
        "clone_path": str(fixture),
        "commit": commit,
        "commit_source": commit_source,
        "rendering": _RENDERING,
        "files": files,
        "bundle_tokens_tiktoken": bundle_tokens,
        "preregistration": PREREGISTRATION,
    }

    metrics_url = root.rstrip("/") + "/metrics"
    records: list[dict[str, Any]] = []
    arms_summary: dict[str, dict[str, Any]] = {}
    per_arm_cache: dict[str, _Counters] = {}
    for arm in ("broadcast", "prefix", "compile"):
        before = _scrape_cache(client, metrics_url)
        stop_sampler = _start_kv_sampler(client, metrics_url)
        started = time.perf_counter()
        arm_records: list[dict[str, Any]] = []
        for role, messages in arms[arm]:
            agent = AGENT_BY_ROLE[role]
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
                        "op": "chat",
                        "arm": arm,
                        "agent": role,
                        "repeat": repeat,
                        "objective": agent.objective,
                        "required": list(agent.required),
                        "forbidden": list(agent.forbidden),
                        "missing": missing,
                        "wrong": wrong,
                        "passed": passed,
                        "error": error,
                        "compiled": arm == "compile",
                        "prompt_tokens": completion.prompt_tokens if completion else 0,
                        "completion_tokens": completion.completion_tokens if completion else 0,
                        "latency_ms": completion.latency_ms if completion else 0.0,
                        "response": completion.text if completion else "",
                        "response_text": (completion.text if completion else "")[:500],
                        "prompt_sha256": completion.prompt_sha256 if completion else "",
                    }
                )
        wall_ms = (time.perf_counter() - started) * 1000
        after = _scrape_cache(client, metrics_url)
        per_arm_cache[arm] = _Counters(
            _delta(before.hits, after.hits), _delta(before.queries, after.queries), stop_sampler()
        )
        records.extend(arm_records)
        arms_summary[arm] = {
            **_totals(arm_records, wall_ms),
            "passed": sum(1 for r in arm_records if r["passed"]),
            "total": len(arm_records),
            "quality": round(sum(1 for r in arm_records if r["passed"]) / len(arm_records), 4),
        }

    receipt: dict[str, Any] = {
        "scenario": "b4_divergent",
        "kind": "live",
        "model": model,
        "upstream": root,
        "started_at": started_at,
        "ts": ts,
        "preregistration": PREREGISTRATION,
        "fixture": fixture_manifest,
        "repeats": repeats,
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
        "criteria": _criteria(arms_summary, per_arm_cache, records, repeats),
        "notes": [
            f"Fixture is REAL repository content: {PREGISTERED_REPO} @ {commit}; the raw "
            f"bundle is the {len(files)}-file slice ({bundle_tokens} tiktoken) rendered with "
            "FILE headers (actual bytes, stable order); per-file sha256 in the fixture so a "
            "fresh clone can audit the exact bytes.",
            "Three arms over one 4-agent (planner/implementer/tester/reviewer) workflow; "
            "broadcast sends the full bundle to every agent, prefix appends a per-role "
            "instruction after the shared bundle (vLLM prefix caching's best case), compile "
            "sends each role's compiled packet via the LangGraph adapter.",
            "Each arm's system content is prefixed with a unique arm tag so the persistent "
            "vLLM prefix cache cannot carry between arms; within an arm the shared bytes "
            "(broadcast: whole system; prefix: bundle; compile: none) drive prefix reuse.",
            "Quality is the same deterministic required+forbidden scorer for every arm "
            "(all required facts present, no forbidden fact present); every role, repeat and "
            "response is reported, pass or fail.",
            "The compile arm compiles client-side (compile_node_packet), not through a "
            "sidecar; its 'compiled' flag is self-computed, not a server header.",
            "Prefix-cache counters come from the upstream Prometheus /metrics (vLLM), parsed "
            "tolerantly (null when unavailable); uncached tokens = billed prompt tokens minus "
            "the prefix-cache hit-token delta. The first request of each arm is a cold prefill.",
            f"n={len(_AGENTS)} roles x {repeats} repeats per arm, temperature=0.0, "
            f"non-streaming, max_tokens={max_tokens}; single GPU, single 1.5B model.",
        ],
    }
    return receipt


def _upstream_url(raw: str) -> str:
    """CF_BASE_URL-style values end in /v1; return the bare upstream root."""
    return raw.rstrip("/")[:-3].rstrip("/") if raw.rstrip("/").endswith("/v1") else raw.rstrip("/")


def _print_dry_run(receipt: dict[str, Any]) -> None:
    print(f"b4_divergent: kind={receipt['kind']} preregistration={receipt['preregistration']}")
    fixture = receipt["fixture"]
    print(
        f"fixture: {fixture['repo']} @ {fixture['commit']} "
        f"({len(fixture['files'])} files, {fixture['bundle_tokens_tiktoken']} tiktoken)"
    )
    print(f"max_model_len: {receipt['max_model_len']}")
    for arm, summary in receipt["arms"].items():
        roles = summary["roles"]
        print(
            f"{arm:<10} prompt_tokens_total={summary['prompt_tokens_total']:>6} "
            f"max_role={summary['max_role_prompt_tokens']:>5} "
            f"fits<{receipt['max_model_len']}={summary['fits_max_model_len']}"
        )
        for role, stats in roles.items():
            print(f"  {role:<12} {stats['prompt_tokens']:>5} tok  {stats['prompt_sha256']}")


def _print_receipt(receipt: dict[str, Any]) -> None:
    if receipt["kind"] == "dry_run":
        _print_dry_run(receipt)
        return
    print(
        f"b4_divergent: kind={receipt['kind']} model={receipt['model']} "
        f"upstream={receipt['upstream']}"
    )
    print(f"{'arm':<10}{'reqs':>5}{'prompt_tok':>11}{'compl_tok':>10}{'mean_s':>8}  quality")
    for name, arm in receipt["arms"].items():
        mean_s = float(arm.get("mean_latency_ms") or 0.0) / 1000
        print(
            f"{name:<10}{arm['requests']:>5}{arm['prompt_tokens_total']:>11}"
            f"{arm['completion_tokens_total']:>10}{mean_s:>8.2f}  "
            f"{arm['passed']}/{arm['total']} ({arm['quality']:.2f})"
        )
    if receipt["cache"]["available"]:
        for name, stats in receipt["cache"]["per_arm"].items():
            print(
                f"cache[{name}]: hits+={stats['prefix_cache_hits_delta']} "
                f"queries+={stats['prefix_cache_queries_delta']} "
                f"kv_peak={stats['kv_cache_usage_peak']}"
            )
    else:
        print("cache: unavailable (upstream has no /metrics) - token criterion unevaluable")
    criteria = receipt["criteria"]
    print(f"verdict: {criteria['verdict']}  (baseline: {criteria['baseline']})")
    for name, section in criteria.items():
        if isinstance(section, dict) and "requirement" in section:
            print(f"  {name}: pass={section['pass']} ({section['requirement']})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.b4_divergent",
        description="Pre-registered B4: divergent-role compiled vs prefix-cache-only.",
    )
    parser.add_argument(
        "--fixture",
        default=os.environ.get("CF_B4_FIXTURE", ""),
        help="path to the httpx clone (env: CF_B4_FIXTURE)",
    )
    parser.add_argument(
        "--upstream",
        default=os.environ.get("CF_UPSTREAM")
        or os.environ.get("CF_BASE_URL")
        or "http://127.0.0.1:8000",
        help="OpenAI-compatible upstream root (env: CF_UPSTREAM / CF_BASE_URL)",
    )
    parser.add_argument("--model", default=os.environ.get("CF_MODEL", "Qwen/Qwen2.5-1.5B-Instruct"))
    parser.add_argument("--dry-run", action="store_true", help="build prompts offline, no network")
    parser.add_argument("--live", action="store_true", help="run against the upstream (later)")
    parser.add_argument("--repeats", type=int, default=3, help="repeats per role per arm")
    parser.add_argument("--max-tokens", type=int, default=48, help="max_tokens per request")
    parser.add_argument("--out", default="", help="receipt path override")
    args = parser.parse_args(argv)

    if args.repeats <= 0 or args.max_tokens <= 0:
        print("b4_divergent: --repeats and --max-tokens must be positive", file=sys.stderr)
        return 2
    if not args.fixture:
        print("b4_divergent: --fixture (or CF_B4_FIXTURE) is required", file=sys.stderr)
        return 2
    fixture = Path(args.fixture)
    if not (fixture / "httpx" / "_api.py").is_file():
        print(f"b4_divergent: {fixture} does not look like an httpx clone", file=sys.stderr)
        return 2

    ts = time.strftime("%Y%m%d_%H%M%S")
    blocks = fact_blocks()
    if args.dry_run:
        rendered, files = render_bundle(fixture)
        commit, commit_source = commit_of(fixture)
        fixture_manifest = {
            "repo": PREGISTERED_REPO,
            "clone_path": str(fixture),
            "commit": commit,
            "commit_source": commit_source,
            "rendering": _RENDERING,
            "files": files,
            "bundle_tokens_tiktoken": count_tokens(rendered),
            "preregistration": PREREGISTRATION,
        }
        receipt = dry_run_payload(rendered, blocks, fixture=fixture_manifest)
    else:
        upstream = _upstream_url(args.upstream)
        with httpx.Client(timeout=900.0, trust_env=False) as client:
            try:
                receipt = run(
                    client,
                    upstream,
                    model=args.model,
                    fixture=fixture,
                    repeats=args.repeats,
                    max_tokens=args.max_tokens,
                    ts=ts,
                )
            except httpx.HTTPError as exc:
                print(f"b4_divergent: request failed: {exc}", file=sys.stderr)
                return 2

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else REPORTS / f"b4_divergent_{ts}.json"
    out.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    _print_receipt(receipt)
    print(f"receipt: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
