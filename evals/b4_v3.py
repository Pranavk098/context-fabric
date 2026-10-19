"""Pre-registered B4 v3 harness — the repo does not fit the window.

Frozen in ``docs/engineering/B4-V3-PREREGISTRATION.md`` before any run. One fixture (the
``encode/httpx`` clone at the pinned commit, the **full** ``httpx/`` package — 23 files,
~64k tiktoken tokens — which does **not** fit ``--max-model-len 16384``), one required +
forbidden scorer for every arm, one GPU session per model.

Three arms, all 20 objectives x 1 repeat in fixed order, ``temperature=0.0``,
``max_tokens=96``, each arm's system content prefixed with a unique leading cache tag so
the persistent vLLM prefix cache cannot carry between arms:

* ``truncate``  — render the full package (sorted paths, FILE headers) and keep the
  leading bytes that fit ``--max-model-len 16384`` minus the objective, the role line and
  the ``max_tokens`` reserve (counted with the Qwen tokenizer, never tiktoken).
* ``retrieval``  — AST chunks (one per top-level function/class + one per module header
  incl. imports), BM25 over the objective, take the top chunks until 4,000 Qwen tokens.
* ``compile``    — the mechanical fabric over the full package with a shared Tier-0
  cartridge (L1), a terse answer contract (L3) and exact import statements, budget 4,000,
  via the LangGraph adapter path. Role divergence comes **only** from a fixed
  file-ownership map (role -> files); no per-question blocks.

The objectives are blind and frozen: ``evals/fixtures/b4v3_objectives.json``. The harness
verifies their sha256 (as documented in ``docs/engineering/B4-V3-OBJECTIVES.md``) at start
and aborts on mismatch.

Run offline (``--dry-run``, no network) to build every arm's prompts, print per-arm Qwen
token counts (tiktoken-flagged when transformers is unavailable) + a per-objective recall
diagnostic for the retrieval and compile packets; ``--live`` sends them to a real
OpenAI-compatible upstream and applies the frozen criteria. Artifact:
``evals/reports/b4_v3_<ts>.json``.
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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from adapters.langgraph import compile_node_packet, node_model_input
from evals.b4_divergent import _chat, _upstream_url
from evals.mech_fabric import (
    _import_statements,
    build_blocks,
    build_project_cartridge,
)
from kernel.cartridge.serializer import Cartridge, count_tokens
from kernel.router.compiler import DEFAULT_AS_OF
from kernel.router.recall import bm25_scores
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
    _Counters,
    _delta,
    _scrape_cache,
    _start_kv_sampler,
    _totals,
)

REPORTS = Path(__file__).parent / "reports"
PREREGISTRATION = "docs/engineering/B4-V3-PREREGISTRATION.md"
OBJECTIVES_FILE = Path(__file__).parent / "fixtures" / "b4v3_objectives.json"
OBJECTIVES_SHA256 = "5b2c11725195d19a5c34e8625d71cd96fb147a711ce80856ab37765092eeeefb"
PREGISTERED_COMMIT = "b5addb64f0161ff6bfe94c124ef76f6a1fba5254"
PREGISTERED_REPO = "https://github.com/encode/httpx"

MAX_MODEL_LEN = 16384
MAX_TOKENS = 96
BUDGET = 4000
REPEATS = 1
# Qwen2.5 chat-template special tokens (im_start/im_end) — a small, constant safety margin
# so the truncate arm never trips vLLM's max_model_len.
_CHAT_OVERHEAD = 16

SCOPE = Scope(tenant_id="default", project_id="httpx-b4", workflow_id="b4v3", task_id="t1")

ROLES = ("planner", "implementer", "tester", "reviewer")

# File ownership map (frozen): role -> files it owns. Blocks derived from an owned file are
# denied to the three other roles; blocks from unowned files and the project-wide cartridge
# are shared. Divergence comes only from this map, never per-question blocks.
OWNERSHIP: dict[str, tuple[str, ...]] = {
    "planner": (
        "httpx/_client.py",
        "httpx/_config.py",
        "httpx/_status_codes.py",
        "httpx/_transports/base.py",
        "httpx/_transports/default.py",
        "httpx/_urls.py",
    ),
    "implementer": (
        "httpx/_decoders.py",
        "httpx/_models.py",
        "httpx/_urls.py",
    ),
    "tester": (
        "httpx/_api.py",
        "httpx/_exceptions.py",
        "httpx/_models.py",
        "httpx/_status_codes.py",
    ),
    "reviewer": (
        "httpx/_auth.py",
        "httpx/_config.py",
        "httpx/_content.py",
        "httpx/_exceptions.py",
        "httpx/_main.py",
        "httpx/_models.py",
        "httpx/_multipart.py",
    ),
}

_RENDERING = (
    "files in listed (sorted) order, raw bytes, joined by a blank line, each prefixed by "
    "'===== FILE: <path> ====='"
)

_ARM_TAGS = {
    "truncate": "B4v3::truncate::",
    "retrieval": "B4v3::retrieval::",
    "compile": "B4v3::compile::",
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

# L3 terse answer contract (B4-EXPLORE lever, kept as the default).
_TERSE = (
    "Reply with ONLY the comma-separated answer values, nothing else: no sentences, "
    "no explanation, no markdown, no code."
)


class AgentSpec(NamedTuple):
    objective_id: str
    role: str
    objective: str
    required: tuple[str, ...]
    forbidden: tuple[str, ...]


def _objectives_sha256(data: bytes) -> str:
    """sha256 of the objectives file, CRLF-normalized (the documented hash is over CRLF;
    the repo enforces LF via .gitattributes, so the check is line-ending independent)."""
    return sha256_hex(data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))


def load_objectives() -> tuple[AgentSpec, ...]:
    """Load the blind objectives, aborting on a sha256 mismatch (never edited here)."""
    data = OBJECTIVES_FILE.read_bytes()
    actual = _objectives_sha256(data)
    if actual != OBJECTIVES_SHA256:
        print(
            f"b4_v3: objectives sha256 mismatch: {actual} != {OBJECTIVES_SHA256}; aborting",
            file=sys.stderr,
        )
        raise SystemExit(2)
    raw = json.loads(data.decode("utf-8"))
    return tuple(
        AgentSpec(o["id"], o["role"], o["objective"], tuple(o["required"]), tuple(o["forbidden"]))
        for o in raw
    )


def stage_of(role: str) -> str:
    return _STAGES[role]


def role_instruction(role: str, stage: str) -> str:
    return f"ROLE: {role}\nSTAGE: {stage}\n{_ROLE_TASKS[role]}"


def _load_qwen_tokenizer(model: str) -> Any | None:
    """Load the Qwen tokenizer offline when transformers is available, else None."""
    try:
        import importlib

        auto = importlib.import_module("transformers").AutoTokenizer
        tok = auto.from_pretrained(model, local_files_only=True)
        tok.encode("probe", add_special_tokens=False)
        return tok
    except Exception:
        return None


class _Counter:
    """Qwen token counter when available offline, else tiktoken (flagged)."""

    def __init__(self, model: str) -> None:
        self.tokenizer: Any | None = _load_qwen_tokenizer(model)
        self.kind = "qwen" if self.tokenizer is not None else "tiktoken"

    def count(self, text: str) -> int:
        if self.tokenizer is not None:
            return int(len(self.tokenizer.encode(text, add_special_tokens=False)))
        return count_tokens(text)

    def truncate(self, text: str, budget: int) -> str:
        """Leading bytes of ``text`` that fit ``budget`` tokens."""
        if budget <= 0:
            return ""
        if self.tokenizer is not None:
            ids: list[int] = self.tokenizer.encode(text, add_special_tokens=False)
            if len(ids) <= budget:
                return text
            return str(self.tokenizer.decode(ids[:budget]))
        import tiktoken

        enc = tiktoken.get_encoding("o200k_base")
        ids = enc.encode(text)
        if len(ids) <= budget:
            return text
        return enc.decode(ids[:budget])


# ---- scorer (frozen): case-insensitive TOKEN-level match -------------------------------

# Tokens are runs of [A-Za-z0-9_.] plus '-' (kept inside e.g. MD5-SESS) and '/' (kept in
# mime types like application/octet-stream).
_SCORE_TOKEN = re.compile(r"[A-Za-z0-9_.\-/]+")


def _tokens(text: str) -> list[str]:
    return _SCORE_TOKEN.findall(text.lower())


def _tokens_v2(text: str) -> list[str]:
    """Scorer-v2 tokens: strip leading/trailing ``. - /`` from each v1 token."""
    return [t for tok in _SCORE_TOKEN.findall(text.lower()) if (t := tok.strip("./-"))]


def _run_present(haystack: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return n > 0 and any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


def _match_longest(
    answer: list[str],
    items: Sequence[str],
    tokenizer: Callable[[str], list[str]] = _tokens,
) -> tuple[set[int], list[bool]]:
    """Match items against answer tokens, longest-first, each match consuming its tokens."""
    consumed = [False] * len(answer)
    matched: set[int] = set()
    order = sorted(range(len(items)), key=lambda i: (-len(tokenizer(items[i])), -len(items[i])))
    for idx in order:
        toks = tokenizer(items[idx])
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


def score_quality(
    text: str, objective: str, required: Sequence[str], forbidden: Sequence[str]
) -> tuple[bool, list[str], list[str]]:
    """Pass iff all required tokens match and no forbidden token matches.

    A forbidden item whose tokens already appear in the objective text is ignored for that
    objective (a restated question must not trip its own distractor). Returns
    ``(passed, missing_required, matched_forbidden)``.
    """
    answer = _tokens(text)
    objective_tokens = _tokens(objective)
    matched_req, consumed = _match_longest(answer, required)
    missing = [required[i] for i in range(len(required)) if i not in matched_req]
    active = [f for f in forbidden if not _run_present(objective_tokens, _tokens(f))]
    remaining = [t for t, c in zip(answer, consumed, strict=True) if not c]
    matched_forb, _ = _match_longest(remaining, active)
    wrong = [active[i] for i in sorted(matched_forb)]
    return (not missing and not wrong, missing, wrong)


def score_quality_v2(
    text: str, objective: str, required: Sequence[str], forbidden: Sequence[str]
) -> tuple[bool, list[str], list[str]]:
    """Scorer v2: same contract as :func:`score_quality`, but tokens strip leading/trailing
    ``. - /`` (e.g. ``"read."`` and ``"read"`` now match). Selected explicitly via
    ``--scorer v2``; the frozen v1 scorer and every past receipt/verdict are unchanged."""
    answer = _tokens_v2(text)
    objective_tokens = _tokens_v2(objective)
    matched_req, consumed = _match_longest(answer, required, _tokens_v2)
    missing = [required[i] for i in range(len(required)) if i not in matched_req]
    active = [f for f in forbidden if not _run_present(objective_tokens, _tokens_v2(f))]
    remaining = [t for t, c in zip(answer, consumed, strict=True) if not c]
    matched_forb, _ = _match_longest(remaining, active, _tokens_v2)
    wrong = [active[i] for i in sorted(matched_forb)]
    return (not missing and not wrong, missing, wrong)


def _fact_present(text: str, fact: str) -> bool:
    """Diagnostic recall: are ``fact``'s tokens a contiguous run in ``text``?"""
    return _run_present(_tokens(text), _tokens(fact))


# ---- fixture rendering / fabric -------------------------------------------------------


def package_files(fixture: Path) -> tuple[str, ...]:
    """Every ``httpx/*.py`` file, sorted relative path (the full package)."""
    pkg = fixture / "httpx"
    return tuple(sorted(p.relative_to(fixture).as_posix() for p in pkg.rglob("*.py")))


def render_package(fixture: Path, files: Sequence[str]) -> tuple[str, list[dict[str, Any]]]:
    """The truncate arm's full source: actual bytes, sorted order, FILE headers."""
    parts: list[str] = []
    file_meta: list[dict[str, Any]] = []
    for rel in files:
        data = (fixture / rel).read_bytes()
        text = data.decode("utf-8", errors="replace")
        parts.append(f"===== FILE: {rel} =====\n{text}")
        file_meta.append(
            {
                "path": rel,
                "sha256": "sha256:" + sha256_hex(data),
                "bytes": len(data),
                "tiktoken": count_tokens(text),
            }
        )
    return "\n\n".join(parts), file_meta


def build_compile_blocks(
    fixture: Path, files: Sequence[str], ownership: dict[str, tuple[str, ...]]
) -> tuple[ContextBlock, ...]:
    """Mechanical fabric with exact import statements + ownership; cartridge artifact is
    dropped here (the caller pins it as a shared Tier-0 cartridge instead)."""
    owner_by_file = {rel: role for role, rels in ownership.items() for rel in rels}
    roles = tuple(ownership)
    out: list[ContextBlock] = []
    for block in build_blocks(fixture, files, import_statements=True):
        if block.block_kind is BlockKind.artifact:
            continue
        owner = owner_by_file.get(block.ref_uri) if block.ref_uri is not None else None
        denied = tuple(r for r in roles if r != owner) if owner else ()
        out.append(block.model_copy(update={"denied_roles": denied}))
    return tuple(out)


def _chunk_block(
    block_id: str, content: str, seq: int, rel: str, symbols: tuple[str, ...]
) -> ContextBlock:
    return ContextBlock(
        block_id=block_id,
        block_kind=BlockKind.fact,
        memory_tier=MemoryTier.ledger,
        content=content,
        ref_uri=rel,
        content_hash="sha256:" + sha256_hex(content.encode("utf-8")),
        token_estimate=count_tokens(content),
        source_event_seq=seq,
        creator=Creator(type=CreatorType.tool, id="b4v3-r"),
        fidelity=Fidelity.extract,
        trust_domain="default",
        symbols_exported=symbols,
        observed_at=DEFAULT_AS_OF,
        valid_from=DEFAULT_AS_OF,
    )


def build_ast_chunks(fixture: Path, files: Sequence[str]) -> list[ContextBlock]:
    """AST chunks for the retrieval arm: one per top-level function/class + one module
    header (docstring + import statements) per file."""
    chunks: list[ContextBlock] = []
    seq = 0
    for rel in files:
        text = (fixture / rel).read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        header = [f"===== FILE: {rel} ====="]
        doc = ast.get_docstring(tree)
        if doc:
            header.append(doc)
        header.extend(_import_statements(tree))
        if len(header) > 1:
            seq += 1
            chunks.append(_chunk_block(f"chunk_{rel}:header", "\n".join(header), seq, rel, ()))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                segment = ast.get_source_segment(text, node)
                if not segment:
                    continue
                seq += 1
                chunks.append(
                    _chunk_block(
                        f"chunk_{rel}:{node.name}",
                        f"===== FILE: {rel} =====\n{segment}",
                        seq,
                        rel,
                        (node.name,),
                    )
                )
    return chunks


# ---- arm construction ----------------------------------------------------------------


@dataclass
class _ArmBundle:
    arms: dict[str, list[tuple[AgentSpec, list[dict[str, str]]]]]
    files: tuple[str, ...]
    counter: _Counter
    rendered: str
    file_meta: list[dict[str, Any]]
    cartridge: Cartridge
    blocks: tuple[ContextBlock, ...]
    chunks: list[ContextBlock]


def _truncate_messages(
    agent: AgentSpec, rendered: str, counter: _Counter, *, tag: str
) -> list[dict[str, str]]:
    role_line = role_instruction(agent.role, stage_of(agent.role))
    fixed = counter.count(f"{tag}\n{role_line}\n") + counter.count(agent.objective)
    budget = MAX_MODEL_LEN - MAX_TOKENS - _CHAT_OVERHEAD - fixed
    prefix = counter.truncate(rendered, budget)
    system = f"{tag}\n{role_line}\n{prefix}"
    return [{"role": "system", "content": system}, {"role": "user", "content": agent.objective}]


def _retrieval_messages(
    agent: AgentSpec, chunks: Sequence[ContextBlock], counter: _Counter, *, tag: str
) -> list[dict[str, str]]:
    scores = bm25_scores(agent.objective, chunks)
    ranked = sorted(chunks, key=lambda c: (-scores.get(c.block_id, 0.0), c.block_id))
    selected: list[ContextBlock] = []
    spent = 0
    for chunk in ranked:
        if scores.get(chunk.block_id, 0.0) <= 0.0:
            continue
        cost = counter.count(chunk.content or "")
        if spent + cost > BUDGET:
            continue
        selected.append(chunk)
        spent += cost
    body = "\n\n".join(c.content or "" for c in selected)
    system = f"{tag}\n{body}"
    return [{"role": "system", "content": system}, {"role": "user", "content": agent.objective}]


def _compile_messages(
    agent: AgentSpec,
    blocks: Sequence[ContextBlock],
    cartridge: Cartridge,
    *,
    tag: str,
    as_of: datetime,
) -> list[dict[str, str]]:
    result = compile_node_packet(
        {"objective": agent.objective, "blocks": blocks},
        scope=SCOPE,
        recipient=Recipient(
            agent_id=agent.objective_id, role=agent.role, stage=stage_of(agent.role)
        ),
        as_of=as_of,
        budget_base=BUDGET,
        cartridge=cartridge,
        output_contract=_TERSE,
    )
    system = f"{tag}\n{node_model_input(result)['content']}"
    return [{"role": "system", "content": system}, {"role": "user", "content": agent.objective}]


def build_arms(
    fixture: Path, objectives: Sequence[AgentSpec], *, model: str, as_of: datetime = DEFAULT_AS_OF
) -> _ArmBundle:
    files = package_files(fixture)
    counter = _Counter(model)
    rendered, file_meta = render_package(fixture, files)
    cartridge = build_project_cartridge(fixture, files)
    blocks = build_compile_blocks(fixture, files, OWNERSHIP)
    chunks = build_ast_chunks(fixture, files)
    arms: dict[str, list[tuple[AgentSpec, list[dict[str, str]]]]] = {
        "truncate": [],
        "retrieval": [],
        "compile": [],
    }
    for agent in objectives:
        arms["truncate"].append(
            (agent, _truncate_messages(agent, rendered, counter, tag=_ARM_TAGS["truncate"]))
        )
        arms["retrieval"].append(
            (agent, _retrieval_messages(agent, chunks, counter, tag=_ARM_TAGS["retrieval"]))
        )
        arms["compile"].append(
            (
                agent,
                _compile_messages(agent, blocks, cartridge, tag=_ARM_TAGS["compile"], as_of=as_of),
            )
        )
    return _ArmBundle(
        arms=arms,
        files=files,
        counter=counter,
        rendered=rendered,
        file_meta=file_meta,
        cartridge=cartridge,
        blocks=blocks,
        chunks=chunks,
    )


# ---- dry run -------------------------------------------------------------------------


def _prompt_stats(messages: list[dict[str, str]], counter: _Counter) -> dict[str, Any]:
    prompt_tokens = sum(counter.count(m["content"]) for m in messages)
    return {
        "prompt_tokens": prompt_tokens,
        "prompt_sha256": "sha256:" + content_hash(messages),
        "system_sha256": "sha256:" + sha256_hex(messages[0]["content"].encode("utf-8")),
        "messages": messages,
    }


def recall_report(bundle: _ArmBundle, objectives: Sequence[AgentSpec]) -> list[dict[str, Any]]:
    """Per-objective recall diagnostic for the retrieval and compile packets (OUTCOME)."""
    rows: list[dict[str, Any]] = []
    for arm, pairs in bundle.arms.items():
        if arm == "truncate":
            continue
        for agent, messages in pairs:
            rendered = messages[0]["content"]
            facts = {fact: _fact_present(rendered, fact) for fact in agent.required}
            rows.append(
                {
                    "objective_id": agent.objective_id,
                    "role": agent.role,
                    "arm": arm,
                    "required": list(agent.required),
                    "facts_in_packet": facts,
                    "all_in_packet": all(facts.values()),
                }
            )
    return rows


def dry_run_payload(
    fixture: Path, objectives: Sequence[AgentSpec], *, model: str, as_of: datetime = DEFAULT_AS_OF
) -> dict[str, Any]:
    """Deterministic offline build of every arm's prompts (no wall clock, no network)."""
    bundle = build_arms(fixture, objectives, model=model, as_of=as_of)
    per_arm: dict[str, dict[str, Any]] = {}
    for arm, pairs in bundle.arms.items():
        objectives_rows: dict[str, dict[str, Any]] = {}
        total = 0
        for agent, messages in pairs:
            stats = _prompt_stats(messages, bundle.counter)
            total += int(stats["prompt_tokens"])
            objectives_rows[agent.objective_id] = {
                "role": agent.role,
                "prompt_tokens": stats["prompt_tokens"],
                "prompt_sha256": stats["prompt_sha256"],
            }
        per_arm[arm] = {
            "prompt_tokens_total": total,
            "max_objective_prompt_tokens": max(
                r["prompt_tokens"] for r in objectives_rows.values()
            ),
            "objectives": objectives_rows,
        }
    return {
        "scenario": "b4_v3",
        "kind": "dry_run",
        "preregistration": PREREGISTRATION,
        "model": model,
        "tokenizer": bundle.counter.kind,
        "max_model_len": MAX_MODEL_LEN,
        "max_tokens": MAX_TOKENS,
        "budget": BUDGET,
        "n_objectives": len(objectives),
        "repeats": REPEATS,
        "n_files": len(bundle.files),
        "bundle_tokens_tiktoken": count_tokens(bundle.rendered),
        "file_meta": bundle.file_meta,
        "arms": per_arm,
        "recall": recall_report(bundle, objectives),
    }


# ---- live run ------------------------------------------------------------------------


def _criteria(
    arms: dict[str, dict[str, Any]], records: Sequence[dict[str, Any]], n_objectives: int
) -> dict[str, Any]:
    t_passes = int(arms["truncate"]["passed"])
    r_passes = int(arms["retrieval"]["passed"])
    c_passes = int(arms["compile"]["passed"])
    t_billed = int(arms["truncate"]["prompt_tokens_total"])
    c_billed = int(arms["compile"]["prompt_tokens_total"])

    def error_rate(arm: str) -> float:
        total = int(arms[arm]["total"])
        errors = sum(1 for rec in records if rec["arm"] == arm and rec["error"])
        return errors / total if total else 0.0

    error_rates = {arm: error_rate(arm) for arm in ("truncate", "retrieval", "compile")}
    unreliable = any(rate > 0.25 for rate in error_rates.values())
    quality_pass = c_passes >= r_passes and c_passes >= t_passes - 2
    token_pass = c_billed * 3 <= t_billed  # C billed <= T billed / 3

    if unreliable:
        verdict = "INCONCLUSIVE"
    elif c_passes < r_passes - 2:
        verdict = "KILL"
    elif quality_pass and token_pass:
        verdict = "PASS"
    else:
        verdict = "MIXED"

    return {
        "verdict": verdict,
        "n_objectives": n_objectives,
        "repeats": REPEATS,
        "quality": {
            "pass": quality_pass,
            "requirement": "compile passes >= retrieval AND >= truncate passes - 2",
            "truncate_passes": t_passes,
            "retrieval_passes": r_passes,
            "compile_passes": c_passes,
        },
        "tokens": {
            "pass": token_pass,
            "requirement": "compile billed prompt tokens <= truncate billed / 3",
            "truncate_billed": t_billed,
            "compile_billed": c_billed,
            "ratio_truncate_over_compile": round(t_billed / c_billed, 2) if c_billed else None,
        },
        "unreliable_error_rate": unreliable,
        "error_rates": error_rates,
    }


def run(
    client: httpx.Client,
    root: str,
    *,
    model: str,
    fixture: Path,
    ts: str,
    scorer: str = "v1",
) -> dict[str, Any]:
    started_at = datetime.now(tz=UTC).isoformat()
    objectives = load_objectives()
    bundle = build_arms(fixture, objectives, model=model)
    score = score_quality_v2 if scorer == "v2" else score_quality

    fixture_manifest = {
        "repo": PREGISTERED_REPO,
        "clone_path": str(fixture),
        "commit": PREGISTERED_COMMIT,
        "commit_source": "preregistered",
        "rendering": _RENDERING,
        "files": bundle.file_meta,
        "bundle_tokens_tiktoken": count_tokens(bundle.rendered),
        "n_files": len(bundle.files),
        "ownership": {role: list(rels) for role, rels in OWNERSHIP.items()},
        "preregistration": PREREGISTRATION,
        "tokenizer": bundle.counter.kind,
    }

    metrics_url = root.rstrip("/") + "/metrics"
    records: list[dict[str, Any]] = []
    arms_summary: dict[str, dict[str, Any]] = {}
    per_arm_cache: dict[str, _Counters] = {}
    for arm in ("truncate", "retrieval", "compile"):
        before = _scrape_cache(client, metrics_url)
        stop_sampler = _start_kv_sampler(client, metrics_url)
        started = time.perf_counter()
        arm_records: list[dict[str, Any]] = []
        for agent, messages in bundle.arms[arm]:
            try:
                completion = _chat(
                    client, root, model=model, messages=messages, max_tokens=MAX_TOKENS
                )
                passed, missing, wrong = score(
                    completion.text, agent.objective, agent.required, agent.forbidden
                )
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
                    "objective": agent.objective,
                    "required": list(agent.required),
                    "forbidden": list(agent.forbidden),
                    "missing": missing,
                    "wrong": wrong,
                    "passed": passed,
                    "error": error,
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
            "errors": sum(1 for r in arm_records if r["error"]),
            "quality": round(sum(1 for r in arm_records if r["passed"]) / len(arm_records), 4)
            if arm_records
            else 0.0,
        }
        hits = per_arm_cache[arm].hits
        uncached = (
            max(int(arms_summary[arm]["prompt_tokens_total"]) - int(round(hits)), 0)
            if hits is not None
            else None
        )
        arms_summary[arm]["uncached_tokens"] = uncached

    receipt: dict[str, Any] = {
        "scenario": "b4_v3",
        "kind": "live",
        "model": model,
        "upstream": root,
        "started_at": started_at,
        "ts": ts,
        "preregistration": PREREGISTRATION,
        "fixture": fixture_manifest,
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
        "criteria": _criteria(arms_summary, records, len(objectives)),
    }
    return receipt


# ---- printing / CLI ------------------------------------------------------------------


def _print_dry_run(receipt: dict[str, Any]) -> None:
    print(
        f"b4_v3: kind=dry_run model={receipt['model']} tokenizer={receipt['tokenizer']} "
        f"preregistration={receipt['preregistration']}"
    )
    print(
        f"fixture: {receipt['n_files']} files, {receipt['bundle_tokens_tiktoken']} tiktoken "
        f"(full package, > max_model_len {receipt['max_model_len']})"
    )
    print(
        f"max_model_len={receipt['max_model_len']} max_tokens={receipt['max_tokens']} "
        f"budget={receipt['budget']} n_objectives={receipt['n_objectives']}"
    )
    for arm, summary in receipt["arms"].items():
        print(
            f"{arm:<10} prompt_tokens_total={summary['prompt_tokens_total']:>7} "
            f"max_objective={summary['max_objective_prompt_tokens']:>6}"
        )
    print("recall (required facts present in the packet — OUTCOME, not a gate):")
    for row in receipt["recall"]:
        marks = " ".join(
            f"{fact}={'Y' if hit else 'N'}" for fact, hit in row["facts_in_packet"].items()
        )
        print(f"  {row['objective_id']:<10} [{row['arm']:<9}] {marks}")


def _print_receipt(receipt: dict[str, Any]) -> None:
    if receipt["kind"] == "dry_run":
        _print_dry_run(receipt)
        return
    print(f"b4_v3: kind=live model={receipt['model']} upstream={receipt['upstream']}")
    print(f"{'arm':<10}{'reqs':>5}{'prompt_tok':>11}{'uncached':>10}{'mean_s':>8}  quality")
    for name, arm in receipt["arms"].items():
        mean_s = float(arm.get("mean_latency_ms") or 0.0) / 1000
        print(
            f"{name:<10}{arm['requests']:>5}{arm['prompt_tokens_total']:>11}"
            f"{str(arm.get('uncached_tokens')):>10}{mean_s:>8.2f}  "
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
        print("cache: unavailable (upstream has no /metrics)")
    criteria = receipt["criteria"]
    print(f"verdict: {criteria['verdict']}")
    for name, section in criteria.items():
        if isinstance(section, dict) and "requirement" in section:
            print(f"  {name}: pass={section['pass']} ({section['requirement']})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.b4_v3",
        description="Pre-registered B4 v3: the repo does not fit the window.",
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
    parser.add_argument(
        "--scorer",
        choices=("v1", "v2"),
        default="v1",
        help="scorer version: v1 = frozen token match; v2 = strip leading/trailing . - /",
    )
    parser.add_argument("--dry-run", action="store_true", help="build prompts offline, no network")
    parser.add_argument("--live", action="store_true", help="run against the upstream")
    parser.add_argument("--out", default="", help="receipt path override")
    args = parser.parse_args(argv)

    if not args.fixture:
        print("b4_v3: --fixture (or CF_B4_FIXTURE) is required", file=sys.stderr)
        return 2
    fixture = Path(args.fixture)
    if not (fixture / "httpx" / "_api.py").is_file():
        print(f"b4_v3: {fixture} does not look like an httpx clone", file=sys.stderr)
        return 2

    ts = time.strftime("%Y%m%d_%H%M%S")
    if args.dry_run:
        objectives = load_objectives()
        receipt = dry_run_payload(fixture, objectives, model=args.model)
    else:
        upstream = _upstream_url(args.upstream)
        with httpx.Client(timeout=900.0, trust_env=False) as client:
            try:
                receipt = run(
                    client, upstream, model=args.model, fixture=fixture, ts=ts, scorer=args.scorer
                )
            except httpx.HTTPError as exc:
                print(f"b4_v3: request failed: {exc}", file=sys.stderr)
                return 2

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else REPORTS / f"b4_v3_{ts}.json"
    out.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    _print_receipt(receipt)
    print(f"receipt: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
