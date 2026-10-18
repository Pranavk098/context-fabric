"""Pre-registered B4 v2 harness — divergent-role compiled context vs prefix-cache-only.

Design, roles, objectives, scorer, arms and success criteria are frozen in
``docs/engineering/B4-PREREGISTRATION-v2.md`` (2026-10-18) before any run. One fixture (the
``encode/httpx`` clone at the pinned commit), one 4-file raw bundle (arms A/B's shared
system content, sized < ``--max-model-len 8192``), a **mechanical** fabric built by
``evals/mech_fabric.build_blocks`` from those same source files (never any objective or
answer key), one required+forbidden scorer for every arm, one GPU session.

Role divergence comes **only** from a fixed file-ownership map (``OWNERSHIP``): each role
owns one file, and blocks built from that file are denied to the other three roles. No
per-question fact blocks. Whether a compiled packet ends up carrying a role's required
facts is an OUTCOME (the dry-run prints a per-objective recall diagnostic), never a
precondition.

Arms (all objectives x ``--repeats`` requests, in a fixed order):

* ``broadcast`` — full raw bundle to every agent (no role identity).
* ``prefix``   — shared raw bundle + role instruction appended (the strongest naive
  baseline: identical leading bytes so vLLM prefix caching gets its best case).
* ``compile``  — per-role compiled packet via the LangGraph adapter
  (``compile_node_packet`` + ``node_model_input``).

Each arm's system content carries a unique leading tag so the persistent vLLM prefix cache
cannot carry between arms. Run offline (``--dry-run``, no network) to build every arm's
prompts, print per-arm prompt token counts + a per-objective recall diagnostic, and record
a sha256 of each prompt; ``--live`` sends them to a real OpenAI-compatible upstream and
applies the frozen criteria.

Artifact: ``evals/reports/b4_divergent_<ts>.json``.
"""

from __future__ import annotations

import argparse
import json
import math
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
from evals.mech_fabric import build_blocks
from kernel.cartridge.serializer import count_tokens
from kernel.router.compiler import DEFAULT_AS_OF
from protocol.canonical import content_hash, sha256_hex
from protocol.models import ContextBlock, Recipient, Scope
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
PREREGISTRATION = "docs/engineering/B4-PREREGISTRATION-v2.md"
MAX_MODEL_LEN = 8192
TOLERANCE_FRACTION = 0.1

SCOPE = Scope(tenant_id="default", project_id="httpx-b4", workflow_id="divergent", task_id="t1")

ROLES = ("planner", "implementer", "tester", "reviewer")

# The fixture bundle: an explicit 4-file slice (preregistration v2 section 2), sized so
# every raw arm fits --max-model-len 8192 (largest measured ~7,880 tokens).
BUNDLE_FILES: tuple[str, ...] = (
    "httpx/_api.py",
    "httpx/_auth.py",
    "httpx/_status_codes.py",
    "httpx/_types.py",
)

# File/area ownership map (fixed in advance): role -> files it owns. Divergence is applied
# by denying every source-derived block to the three roles that do not own its file. This
# is the ONLY source of role divergence; nothing is set per question.
OWNERSHIP: dict[str, tuple[str, ...]] = {
    "planner": ("httpx/_api.py",),
    "implementer": ("httpx/_auth.py",),
    "tester": ("httpx/_status_codes.py",),
    "reviewer": ("httpx/_types.py",),
}

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
    "implementer": "Implement by naming the exact symbol from the context above.",
    "tester": "Test by reading the exact value from the context above.",
    "reviewer": "Review by naming the exact type from the context above.",
}

_STAGES = {
    "planner": "plan",
    "implementer": "implement",
    "tester": "test",
    "reviewer": "review",
}


class AgentSpec(NamedTuple):
    objective_id: str
    role: str
    objective: str
    required: tuple[str, ...]
    forbidden: tuple[str, ...]

    @property
    def stage(self) -> str:
        return _STAGES[self.role]


# role, objective, required facts (all), forbidden wrong facts (none) — preregistration
# v2 section 3 (frozen). Required facts are answerable from the RAW bundle (the only
# construction guarantee); forbidden facts are plausible wrong answers also present in the
# source. "2+" marks objectives that need two distinct facts (the "harder" half).
_AGENTS: tuple[AgentSpec, ...] = (
    # planner owns httpx/_api.py
    AgentSpec(
        "planner-1",
        "planner",
        "Which two `_X` modules does `httpx/_api.py` import its `Client` and `Response` "
        "classes from? Answer `_X, _Y`.",
        ("_client", "_models"),
        ("_urls",),
    ),
    AgentSpec(
        "planner-2",
        "planner",
        "Which two `_X` modules does `httpx/_api.py` import `DEFAULT_TIMEOUT_CONFIG` and "
        "`URL` from? Answer `_X, _Y`.",
        ("_config", "_urls"),
        ("_types",),
    ),
    AgentSpec(
        "planner-3",
        "planner",
        "Which two functions in `_api.py` are the generic engine that every verb helper "
        "delegates to, and the streaming context-manager alternative? Answer `name, name`.",
        ("request", "stream"),
        ("delete",),
    ),
    # implementer owns httpx/_auth.py
    AgentSpec(
        "implementer-1",
        "implementer",
        "Which two class-level flags does the `Auth` base class in `_auth.py` declare, so "
        "subclasses can signal that the request or response body must be read? "
        "Answer `flag, flag`.",
        ("requires_request_body", "requires_response_body"),
        ("auth_flow",),
    ),
    AgentSpec(
        "implementer-2",
        "implementer",
        "Which two auth classes in `_auth.py` adapt a plain callable and a netrc file, "
        "respectively? Answer `class, class`.",
        ("functionauth", "netrcauth"),
        ("digestauth",),
    ),
    AgentSpec(
        "implementer-3",
        "implementer",
        "Which auth class in `_auth.py` parses a `WWW-Authenticate: Digest` challenge and "
        "supports the `MD5`, `SHA`, `SHA-256` and `SHA-512` algorithms?",
        ("digestauth",),
        ("basicauth",),
    ),
    # tester owns httpx/_status_codes.py
    AgentSpec(
        "tester-1",
        "tester",
        "In `_status_codes.py`, give the two integer status codes for `IM_A_TEAPOT` and "
        "`TOO_MANY_REQUESTS`. Answer `int, int`.",
        ("418", "429"),
        ("451",),
    ),
    AgentSpec(
        "tester-2",
        "tester",
        "In `_status_codes.py`, give the reason phrases for status codes 404 and 503. "
        "Answer `phrase, phrase`.",
        ("not found", "service unavailable"),
        ("bad gateway",),
    ),
    AgentSpec(
        "tester-3",
        "tester",
        "Which `codes` classmethod in `_status_codes.py` returns `True` for both 4xx and "
        "5xx status codes?",
        ("is_error",),
        ("is_client_error",),
    ),
    # reviewer owns httpx/_types.py
    AgentSpec(
        "reviewer-1",
        "reviewer",
        "Which two type aliases in `_types.py` name the accepted types for a request URL "
        "and for request headers? Answer `alias, alias`.",
        ("urltypes", "headertypes"),
        ("cookietypes",),
    ),
    AgentSpec(
        "reviewer-2",
        "reviewer",
        "Which two type aliases in `_types.py` are the request and response body types, "
        "both `Union[str, bytes, Iterable[bytes], AsyncIterable[bytes]]`? "
        "Answer `alias, alias`.",
        ("requestcontent", "responsecontent"),
        ("requestdata",),
    ),
    AgentSpec(
        "reviewer-3",
        "reviewer",
        "Which class in `_types.py` is the synchronous byte-stream base, defining `__iter__` "
        "and `close`?",
        ("syncbytestream",),
        ("asyncbytestream",),
    ),
)

AGENT_BY_ID = {agent.objective_id: agent for agent in _AGENTS}


def build_mech_blocks(root: Path) -> tuple[ContextBlock, ...]:
    """Mechanical fabric for the fixture, with ownership applied (see module docstring)."""
    owner_by_file = {rel: role for role, rels in OWNERSHIP.items() for rel in rels}
    roles = tuple(OWNERSHIP)
    owned: list[ContextBlock] = []
    for block in build_blocks(root, BUNDLE_FILES):
        owner = owner_by_file.get(block.ref_uri) if block.ref_uri is not None else None
        denied = tuple(r for r in roles if r != owner) if owner else ()
        owned.append(block.model_copy(update={"denied_roles": denied}))
    return tuple(owned)


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


def verify_raw_answerable(rendered: str) -> None:
    """Construction check (preregistration v2): every required fact is in the raw bundle.

    This is the ONLY construction check — it asserts arms A/B can answer, never that a
    compiled packet carries the facts (that is an outcome, not a precondition).
    """
    bundle = rendered.lower()
    problems: list[str] = []
    for agent in _AGENTS:
        for key in agent.required:
            if key.lower() not in bundle:
                problems.append(
                    f"required {key!r} for {agent.objective_id} missing from raw bundle"
                )
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
            recipient=Recipient(agent_id=agent.objective_id, role=agent.role, stage=agent.stage),
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
) -> dict[str, list[tuple[AgentSpec, list[dict[str, str]]]]]:
    """Every arm's prompts, in fixed objective order; (agent, messages) per request."""
    return {
        arm: [
            (agent, node_messages(arm, agent, raw_bundle=raw_bundle, blocks=blocks, as_of=as_of))
            for agent in _AGENTS
        ]
        for arm in ("broadcast", "prefix", "compile")
    }


def recall_report(
    arms: dict[str, list[tuple[AgentSpec, list[dict[str, str]]]]],
) -> list[dict[str, Any]]:
    """Per-objective recall diagnostic: which required facts made it into the compiled packet.

    Pure OUTCOME (no model, no abort) — the compiled packet is an input built offline.
    """
    rows: list[dict[str, Any]] = []
    for agent, messages in arms["compile"]:
        rendered = messages[0]["content"].lower()
        facts = {fact: fact.lower() in rendered for fact in agent.required}
        rows.append(
            {
                "objective_id": agent.objective_id,
                "role": agent.role,
                "required": list(agent.required),
                "facts_in_packet": facts,
                "all_in_packet": all(facts.values()),
            }
        )
    return rows


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
    verify_raw_answerable(raw_bundle)
    arms = build_arms(raw_bundle, blocks, as_of=as_of)
    per_arm: dict[str, dict[str, Any]] = {}
    for arm, pairs in arms.items():
        roles: dict[str, dict[str, Any]] = {}
        total = 0
        for agent, messages in pairs:
            stats = _prompt_stats(messages)
            total += int(stats["prompt_tokens"])
            roles[agent.objective_id] = {
                "role": agent.role,
                "objective": agent.objective,
                **{k: v for k, v in stats.items() if k != "messages"},
                "messages": stats["messages"],
            }
        per_arm[arm] = {
            "prompt_tokens_total": total,
            "max_objective_prompt_tokens": max(r["prompt_tokens"] for r in roles.values()),
            "fits_max_model_len": all(r["prompt_tokens"] < MAX_MODEL_LEN for r in roles.values()),
            "objectives": roles,
        }
    return {
        "scenario": "b4_divergent",
        "kind": "dry_run",
        "preregistration": PREREGISTRATION,
        "fixture": fixture,
        "max_model_len": MAX_MODEL_LEN,
        "n_objectives": len(_AGENTS),
        "repeats": 3,
        "arms": per_arm,
        "recall": recall_report(arms),
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
    n = len(_AGENTS) * repeats
    tolerance = math.ceil(TOLERANCE_FRACTION * n)
    a_passes = int(arms["broadcast"]["passed"])
    b_passes = int(arms["prefix"]["passed"])
    c_passes = int(arms["compile"]["passed"])
    b_uncached = _uncached(int(arms["prefix"]["prompt_tokens_total"]), per_arm_cache["prefix"].hits)
    c_uncached = _uncached(
        int(arms["compile"]["prompt_tokens_total"]), per_arm_cache["compile"].hits
    )

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
    quality_ok = c_passes >= b_passes - tolerance
    unreliable = error_rate("prefix") > 0.25 or error_rate("compile") > 0.25
    token_pass = token_criterion.get("pass")
    if unreliable:
        verdict = "INCONCLUSIVE"
    elif c_passes < b_passes - tolerance:
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
        "n_objectives": len(_AGENTS),
        "repeats": repeats,
        "n": n,
        "tolerance_fraction": TOLERANCE_FRACTION,
        "tolerance_requests": tolerance,
        "quality": {
            "pass": quality_ok,
            "requirement": f"compile passes >= prefix passes - {tolerance}",
            "broadcast_passes": a_passes,
            "prefix_passes": b_passes,
            "compile_passes": c_passes,
            "total": n,
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
    blocks = build_mech_blocks(fixture)
    verify_raw_answerable(rendered)
    commit, commit_source = commit_of(fixture)
    arms = build_arms(rendered, blocks)

    fixture_manifest = {
        "repo": PREGISTERED_REPO,
        "clone_path": str(fixture),
        "commit": commit,
        "commit_source": commit_source,
        "rendering": _RENDERING,
        "files": files,
        "bundle_tokens_tiktoken": bundle_tokens,
        "ownership": {role: list(rels) for role, rels in OWNERSHIP.items()},
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
        for agent, messages in arms[arm]:
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
                        "objective_id": agent.objective_id,
                        "agent": agent.role,
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
        "recall": recall_report(arms),
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
            "The compile arm's fabric is built MECHANICALLY by evals/mech_fabric.build_blocks "
            "from the same source files (signatures, docstrings, assignments, imports, "
            "cartridge); it never reads any objective or answer key. Role divergence comes "
            "only from the fixed file-ownership map (each role owns one file).",
            "Three arms over a 12-objective, 4-role (planner/implementer/tester/reviewer) "
            "workload; broadcast sends the full bundle to every agent, prefix appends a "
            "per-role instruction after the shared bundle (vLLM prefix caching's best case), "
            "compile sends each role's compiled packet via the LangGraph adapter.",
            "Each arm's system content is prefixed with a unique arm tag so the persistent "
            "vLLM prefix cache cannot carry between arms; within an arm the shared bytes "
            "(broadcast: whole system; prefix: bundle; compile: none) drive prefix reuse.",
            "Quality is the same deterministic required+forbidden scorer for every arm "
            "(all required facts present, no forbidden fact present); every objective, "
            "repeat and response is reported, pass or fail. Forbidden facts are plausible "
            "wrong answers from the source; whether a compiled packet carries one is an "
            "outcome, not a precondition.",
            "The compile arm compiles client-side (compile_node_packet), not through a "
            "sidecar; its 'compiled' flag is self-computed, not a server header.",
            "Prefix-cache counters come from the upstream Prometheus /metrics (vLLM), parsed "
            "tolerantly (null when unavailable); uncached tokens = billed prompt tokens minus "
            "the prefix-cache hit-token delta. The first request of each arm is a cold prefill.",
            f"n={len(_AGENTS)} objectives x {repeats} repeats per arm, temperature=0.0, "
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
    print(f"max_model_len: {receipt['max_model_len']}  n_objectives: {receipt['n_objectives']}")
    for arm, summary in receipt["arms"].items():
        objectives = summary["objectives"]
        print(
            f"{arm:<10} prompt_tokens_total={summary['prompt_tokens_total']:>6} "
            f"max_objective={summary['max_objective_prompt_tokens']:>5} "
            f"fits<{receipt['max_model_len']}={summary['fits_max_model_len']}"
        )
        for objective_id, stats in objectives.items():
            print(f"  {objective_id:<14} {stats['prompt_tokens']:>5} tok  {stats['prompt_sha256']}")
    print("recall (required facts present in the compiled packet — OUTCOME, not a gate):")
    for row in receipt["recall"]:
        marks = " ".join(
            f"{fact}={'Y' if hit else 'N'}" for fact, hit in row["facts_in_packet"].items()
        )
        print(f"  {row['objective_id']:<14} [{row['role']:<11}] {marks}")


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
        description="Pre-registered B4 v2: divergent-role compiled vs prefix-cache-only.",
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
    parser.add_argument("--repeats", type=int, default=3, help="repeats per objective per arm")
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
    blocks = build_mech_blocks(fixture)
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
            "ownership": {role: list(rels) for role, rels in OWNERSHIP.items()},
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
