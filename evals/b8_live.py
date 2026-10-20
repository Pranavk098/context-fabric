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
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import lru_cache
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

# Reserve before starting a task x arm so a run is never cut off mid-flight (audit F3).
# A finished run costs 153k-503k; 550k is the observed max plus ~10% headroom.
RUN_RESERVE = 550_000

# ``SWE-bench/SWE-bench_Verified`` (not the ``princeton-nlp`` mirror) is the snapshot that
# still carries the ``eval_script``/``log_parser``/``eval_type``/``image`` columns
# ``swebench.harness.run_evaluation`` (5.0.2) needs to build a TestSpec.
DATASET = "SWE-bench/SWE-bench_Verified"
# The cached snapshot revision (audit F15): pinning it stops a silent upstream change from
# shifting the seeded sample.
DATASET_REVISION = "78f471bf655a3137b2e8a75af1501690ec009ec3"
DEFAULT_SEED = 20261019
# The pilot is the frozen n=10 sample; the default run is the pilot, so the frozen-sample
# assert fires by default (audit N8). A smoke passes an explicit ``--n 30``.
DEFAULT_N = 10

# The frozen n=10 sample (seed 20261019). Asserted at startup so a dataset drift cannot
# silently change which tasks run (audit F15).
FROZEN_INSTANCE_IDS = [
    "django__django-11532",
    "django__django-12262",
    "django__django-13279",
    "django__django-14915",
    "django__django-15851",
    "matplotlib__matplotlib-26208",
    "scikit-learn__scikit-learn-14496",
    "sphinx-doc__sphinx-8035",
    "sympy__sympy-12481",
    "sympy__sympy-19346",
]

# The v3-specific grader run_id. v2 used ``b8_<arm>`` and its stale ``report.json`` files
# are reused by swebench's cache, so v3 must grade under a name v2 never wrote (audit B1).
RUN_ID_PREFIX = "b8v3"

REPORTS = Path(__file__).parent / "reports"
LEDGER_DIR = REPORTS / "b8_ledger"

SCOPE = Scope(tenant_id="default", project_id="b8-live", workflow_id="swebench", task_id="t1")
RECIPIENT = Recipient(agent_id="subagent-solver", role="writer", stage="solve")

_REPO_CARTRIDGE_BUDGET = 600


def grader_run_id(arm: str) -> str:
    """The v3-specific swebench ``run_id`` for an arm (audit B1)."""
    return f"{RUN_ID_PREFIX}_{arm}"


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
    budget: int = HANDOFF_BUDGET,
) -> str:
    if arm == "A":
        return handoff_arm_a(repo_root, candidate_files, budget=budget)
    if arm == "B":
        return handoff_arm_b(
            repo_root, candidate_files, objective, project_id=project_id, budget=budget
        )
    if arm == "C":
        return handoff_arm_c(
            repo_root, candidate_files, objective, project_id=project_id, budget=budget
        )
    raise ValueError(f"unknown arm {arm!r}")


# ------------------------------------------------------------------ shared task header


def build_task_header(problem_statement: str, subagent_query: str, parent_notes: str) -> str:
    """The identical task preamble every arm receives before its hand-off (audit F1).

    ``problem_statement``, then ``subagent_query``, then ``parent_notes``, each clearly
    delimited. This header is the same bytes for arms A, B and C and is NOT counted inside
    the 8,192-token hand-off slot.
    """
    parts = [f"===== TASK =====\n{problem_statement.strip()}"]
    if subagent_query.strip():
        parts.append(f"===== SUB-AGENT QUERY =====\n{subagent_query.strip()}")
    if parent_notes.strip():
        parts.append(f"===== PARENT NOTES =====\n{parent_notes.strip()}")
    return "\n\n".join(parts)


def build_subagent_task(header: str, handoff: str) -> str:
    """The full sub-agent ``task``: the shared header, then this arm's hand-off."""
    return f"{header}\n\n{handoff}"


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
    """Load the SWE-bench Verified ``split`` (needs the ``datasets`` extra), pinned to the
    cached revision (audit F15)."""
    from datasets import load_dataset  # local: WSL-only dependency

    return list(load_dataset(DATASET, split=split, revision=DATASET_REVISION))


@lru_cache(maxsize=1)
def harness_sha() -> str:
    """The git SHA of the harness checkout that produced the current parent/receipt rows."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def assert_frozen_sample(sampled: Sequence[dict[str, Any]], seed: int, n: int) -> None:
    """Refuse to run when the seeded n=10 draw drifts from the frozen list (audit F15)."""
    if seed != DEFAULT_SEED or n != 10:
        return
    ids = [instance["instance_id"] for instance in sampled]
    if ids != FROZEN_INSTANCE_IDS:
        raise SystemExit(f"sample drifted from the frozen list (seed {DEFAULT_SEED}, n=10): {ids}")


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


def _file_signature(repo_root: Path, rel: str) -> str:
    """A one-line summary of a file's exported symbols (``(no exports)`` when none)."""
    symbols = extract_source(rel, _read(repo_root, rel))
    return "; ".join(s.signature for s in symbols.exported) or "(no exports)"


def _bm25_file_ranking(repo_root: Path, files: Sequence[str], query: str) -> list[str]:
    """BM25-rank every file against ``query`` (max chunk score per file, then path)."""
    chunks = build_ast_chunks(repo_root, list(files))
    scores = bm25_scores(query, chunks)
    by_file: dict[str, float] = {}
    for chunk in chunks:
        if not chunk.ref_uri:
            continue
        score = scores.get(chunk.block_id, 0.0)
        by_file[chunk.ref_uri] = max(by_file.get(chunk.ref_uri, 0.0), score)
    return sorted(files, key=lambda rel: (-by_file.get(rel, 0.0), rel))


def _bm25_file_listing(
    repo_root: Path, files: Sequence[str], query: str, budget_tokens: int = 4000
) -> list[str]:
    """BM25-ranked ``path: signature`` lines for the parent prompt, within the budget.

    The parent sees the top files ranked against the problem statement (not an alphabetical
    prefix), each with a short exported-symbol signature (audit F2).
    """
    lines: list[str] = []
    spent = 0
    for rel in _bm25_file_ranking(repo_root, files, query):
        line = f"{rel}: {_file_signature(repo_root, rel)}"
        cost = count_tokens(line) + 1  # newline join
        if spent + cost > budget_tokens:
            break
        lines.append(line)
        spent += cost
    return lines


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


def chat_completions_url(base_url: str) -> str:
    """``<base>/v1/chat/completions`` exactly once, whether ``base_url`` ends in ``/v1`` or not."""
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


class _ParentCapError(Exception):
    """The parent localization was refused by the daily-cap gate (retry tomorrow, F14)."""


def _parent_llm_reply(base_url: str, model: str, prompt: str) -> dict[str, Any]:
    """One parent LLM turn through the sidecar; raises on any unusable reply.

    The sidecar enforces the model allow-list and records the usage in the daily ledger,
    exactly like the sub-agent's requests. The upstream key is read from the environment
    and sent only as an ``Authorization`` header (never in the body, never logged).

    A daily-cap 400 raises :class:`_ParentCapError` (not ``HTTPStatusError``) so the caller
    can distinguish "out of budget, retry tomorrow" from a genuine failure (audit F14).
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not set; cannot authenticate the parent call")
    with httpx.Client(timeout=120.0, trust_env=False) as client:
        response = client.post(
            chat_completions_url(base_url),
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": TEMPERATURE,
                "max_tokens": 512,
            },
        )
        if response.status_code == 400:
            try:
                code = response.json().get("error", {}).get("code")
            except (ValueError, AttributeError):
                code = None
            if code == "daily_cap_reached":
                raise _ParentCapError()
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise ValueError("parent reply had no text content")
    parsed = _parse_parent_json(content)
    if parsed is None:
        raise ValueError("parent reply was not a JSON object")
    if not isinstance(parsed.get("subagent_query"), str) or not parsed["subagent_query"].strip():
        raise ValueError("parent reply had no subagent_query")
    return parsed


def _is_transient_http_error(exc: BaseException) -> bool:
    """True for a parent transport fault worth retrying later: timeouts, connect errors, 5xx.

    A 5xx, timeout or connect failure does not mean the parent gave a bad reply; it means the
    call never completed, so it must not be cached as ``llm_failed`` (audit N3). A 4xx (other
    than the daily-cap gate handled earlier) and any parse/content ``ValueError`` are
    non-transient and stay excluded.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
        ),
    )


def localize_parent(
    instance: dict[str, Any],
    repo_root: Path,
    *,
    model: str,
    base_url: str,
    top_k: int = 5,
) -> dict[str, Any]:
    """One frozen Parent localization turn (LLM through the sidecar, retried once).

    Returns ``{"parent_status", "error", "subagent_query", "parent_notes",
    "candidate_files", "listing_shown", "prompt_tokens", "prompt_sha256"}``.
    ``parent_status`` is ``"llm_ok"`` when the mini model returned a parseable reply with a
    non-empty ``subagent_query``; ``"cap_stopped"`` when the daily-cap gate refused the call
    (not cached — retried tomorrow, F14); ``"llm_retry"`` on a transient transport fault
    (timeout / connect error / 5xx — not cached, retried on a later invocation, N3); else
    ``"llm_failed"`` with ``error`` set (a bad reply, cached and excluded). BM25 only fills
    candidate gaps the model left; a failed parent is never re-written as a BM25-only
    hand-off. The parent chooses from BM25-ranked candidate files (F2), not an alphabetical
    list.
    """
    problem = instance["problem_statement"]
    files = list_python_files(repo_root)
    listing = _bm25_file_listing(repo_root, files, problem, budget_tokens=4000)
    listing_shown = [line.split(": ", 1)[0] for line in listing]
    prompt = (
        "You are a senior engineer triaging a GitHub issue for a coding sub-agent. "
        "Return ONLY one JSON object with keys: candidate_files (a list of up to "
        f"{top_k} repository file paths, in rank order, most likely to need editing), "
        "subagent_query (one or two sentences directing the fix), parent_notes (a short "
        "summary of the suspected root cause and a reproduction command).\n\n"
        "Repository python files, ranked by BM25 relevance to the issue "
        "(path: exported-symbol signature):\n"
        f"{chr(10).join(listing)}\n\n"
        f"Issue:\n{problem}\n"
    )
    parsed: dict[str, Any] | None = None
    error = ""
    transient = False
    for _ in range(2):  # one retry on any failure
        try:
            parsed = _parent_llm_reply(base_url, model, prompt)
            break
        except _ParentCapError:
            return {
                "parent_status": "cap_stopped",
                "error": "daily cap reached during parent localization",
                "subagent_query": "",
                "parent_notes": "",
                "candidate_files": [],
                "listing_shown": listing_shown,
                "prompt_tokens": count_tokens(prompt),
                "prompt_sha256": sha256_hex(prompt.encode("utf-8")),
            }
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            transient = False
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"
            transient = _is_transient_http_error(exc)
    if parsed is None:
        return {
            "parent_status": "llm_retry" if transient else "llm_failed",
            "error": error,
            "subagent_query": "",
            "parent_notes": "",
            "candidate_files": [],
            "listing_shown": listing_shown,
            "prompt_tokens": count_tokens(prompt),
            "prompt_sha256": sha256_hex(prompt.encode("utf-8")),
        }
    candidate_files = [rel for rel in parsed.get("candidate_files", []) if isinstance(rel, str)]
    subagent_query = parsed["subagent_query"].strip()
    parent_notes = parsed["parent_notes"] if isinstance(parsed.get("parent_notes"), str) else ""
    # Only keep candidate paths that actually exist; BM25 fills the gap (never leaks gold).
    # De-duplicate so a repeated path does not consume a candidate seat (audit F9).
    existing = {f for f in files}
    candidate_files = list(dict.fromkeys(rel for rel in candidate_files if rel in existing))
    for rel in _bm25_candidate_files(repo_root, files, problem, top_k):
        if rel not in candidate_files:
            candidate_files.append(rel)
        if len(candidate_files) >= top_k:
            break
    return {
        "parent_status": "llm_ok",
        "error": "",
        "subagent_query": subagent_query,
        "parent_notes": parent_notes,
        "candidate_files": candidate_files[:top_k],
        "listing_shown": listing_shown,
        "prompt_tokens": count_tokens(prompt),
        "prompt_sha256": sha256_hex(prompt.encode("utf-8")),
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
    """Produce the frozen Parent hand-off once and cache it to disk (JSON).

    The cache records provenance (model, prompt hash, harness git sha, timestamp, the
    BM25-ranked listing shown) so a changed model or prompt cannot silently reuse old
    parents (audit F2/F14). A ``cap_stopped`` or ``llm_retry`` parent is never cached, so it
    is re-attempted next day / next invocation instead of permanently excluding the task
    (audit F14/N3).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{instance['instance_id']}.json"
    if path.is_file() and not force:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    result = localize_parent(instance, repo_root, model=model, base_url=base_url)
    if result.get("parent_status") in ("cap_stopped", "llm_retry"):
        return result
    result["model"] = model
    result["created_at"] = datetime.now(tz=UTC).isoformat()
    result["harness_sha"] = harness_sha()
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


def _is_cap_error(exc: BaseException) -> bool:
    """True when ``exc`` is the sidecar's daily-cap 400 (``daily_cap_reached``)."""
    message = str(exc)
    return "daily_cap_reached" in message or "CF_B8_DAILY_CAP" in message


def _classify_exception(exc: BaseException) -> tuple[str, str]:
    """``(exit_status, sidecar_error)`` for an exception from ``agent.run``.

    A daily-cap 400 → ``cap_stopped``; a context-window overflow →
    ``ContextWindowExceededError``; anything else (a transient network/API/Docker fault)
    → ``tooling_failure``, which is not written to predictions and is eligible for the
    single allowed re-run (audit F12 / AU4-13).
    """
    if _is_cap_error(exc):
        return "cap_stopped", ""
    if type(exc).__name__ == "ContextWindowExceededError":
        return "ContextWindowExceededError", ""
    return "tooling_failure", f"{type(exc).__name__}: {exc}"


def _salvage_git_diff(environment: Any) -> str:
    """Best-effort working-tree ``git diff`` from the sub-agent container, else ``""``."""
    try:
        out = environment.execute({"command": "git diff"})
        return out["output"] if out.get("returncode") == 0 else ""
    except Exception:
        return ""


def recover_patch(exit_status: str, submission: str, salvaged: str) -> tuple[str, str]:
    """The uniform patch-recovery rule for every non-cap exit status (audit F5).

    Returns ``(patch, source)``: ``cap_stopped`` never recovers (it re-runs from scratch);
    otherwise an agent submission wins, then the salvaged working-tree ``git diff``, else
    ``("", "")``. ``source`` records how the patch was obtained.
    """
    if exit_status == "cap_stopped":
        return "", ""
    if submission:
        return submission, "agent_submission"
    if salvaged:
        return salvaged, "working_tree_git_diff"
    return "", ""


def run_subagent(
    instance: dict[str, Any],
    task_text: str,
    *,
    model: str,
    base_url: str,
    output_path: Path,
    step_limit: int = STEP_LIMIT,
) -> dict[str, Any]:
    """Run one mini-swe-agent sub-agent in Docker through ``cfx serve``; return the receipt.

    ``task_text`` is the shared task header plus this arm's hand-off (audit F1). Returns
    ``{exit_status, steps, submission, patch_source, sidecar_error, wall_time_s}``. A context
    overflow surfaces as ``exit_status == "ContextWindowExceededError"``; a daily-cap stop as
    ``"cap_stopped"`` (empty submission, no patch recovery — it re-runs from scratch); any
    other infrastructure exception as ``"tooling_failure"`` (not written to predictions, so
    ``--resume`` re-runs it — audit F12).

    Patch recovery is uniform across every non-cap exit status (audit F5): a run that did
    not submit a patch takes the working-tree ``git diff``, and ``patch_source`` records how
    the patch was obtained (``agent_submission`` | ``working_tree_git_diff`` | ``""``).
    """
    import litellm  # local: WSL-only dependency
    from minisweagent.agents.default import DefaultAgent  # local: WSL-only dependency
    from minisweagent.config import get_config_from_spec
    from minisweagent.environments.docker import DockerEnvironment
    from minisweagent.models.litellm_model import LitellmModel
    from minisweagent.run.benchmarks.swebench import get_swebench_docker_image_name

    class _B8LitellmModel(LitellmModel):  # type: ignore[misc]
        # A cap 400 maps to litellm.BadRequestError; abort instead of the 10x backoff retry.
        abort_exceptions = [*LitellmModel.abort_exceptions, litellm.exceptions.BadRequestError]

    cfg = get_config_from_spec("swebench.yaml")
    agent_cfg = dict(cfg["agent"])
    env_cfg = dict(cfg["environment"])
    model_cfg = dict(cfg["model"])
    env_cfg.pop("environment_class", None)

    environment = None
    agent = None
    exit_status = ""
    submission = ""
    patch_source = ""
    sidecar_error = ""
    started = time.perf_counter()
    try:
        environment = DockerEnvironment(image=get_swebench_docker_image_name(instance), **env_cfg)
        model_obj = _B8LitellmModel(
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
            step_limit=step_limit,
            cost_limit=0.0,
            output_path=output_path,
        )
        info = agent.run(task=task_text)
        exit_status = str(info.get("exit_status", ""))
        submission = str(info.get("submission", ""))
    except Exception as exc:  # ContextWindowExceededError and other aborts propagate here
        exit_status, sidecar_error = _classify_exception(exc)
    finally:
        salvaged = ""
        if environment is not None and exit_status != "cap_stopped" and not submission:
            salvaged = _salvage_git_diff(environment)
        submission, patch_source = recover_patch(exit_status, submission, salvaged)
        if environment is not None:
            environment.cleanup()
    return {
        "exit_status": exit_status,
        "steps": agent.n_calls if agent is not None else 0,
        "submission": submission,
        "patch_source": patch_source,
        "sidecar_error": sidecar_error,
        "wall_time_s": round(time.perf_counter() - started, 3),
    }


# ----------------------------------------------------------------------------- grading


def _pin_dataset_offline() -> None:
    """Force the HF loader to the cached (pinned) snapshot (audit N7).

    ``swebench``'s ``load_swebench_dataset`` takes no revision, so running the loader offline
    makes the grader read the same cached ``DATASET_REVISION`` snapshot that ``load_instances``
    pins, instead of silently pulling a newer upstream revision.
    """
    os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")


def stale_grader_report_paths(
    run_id: str,
    model: str,
    instance_ids: Sequence[str],
    logs_dir: Path | None = None,
) -> list[Path]:
    """Existing official ``report.json`` paths swebench would reuse without re-grading.

    swebench's ``run_instance`` returns a cached ``report.json`` when it exists, so grading a
    new prediction under a ``run_id`` that already has one would silently keep the stale
    result (audit B1). The grader refuses instead.
    """
    logs_dir = logs_dir or Path("logs") / "run_evaluation"
    return [
        logs_dir / run_id / model / instance_id / "report.json"
        for instance_id in instance_ids
        if (logs_dir / run_id / model / instance_id / "report.json").is_file()
    ]


def grade(
    predictions_path: Path,
    *,
    run_id: str,
    max_workers: int = 2,
    reports_dir: Path = REPORTS,
) -> dict[str, int]:
    """Grade a per-arm SWE-bench predictions JSONL via the harness Python API (Docker).

    ``DATASET`` is the ``SWE-bench/SWE-bench_Verified`` snapshot that still carries the
    ``eval_script``/``log_parser``/``eval_type``/``image`` columns ``make_test_spec``
    needs (the ``princeton-nlp`` mirror dropped them). The image is re-stamped from
    ``expected_image`` so it stays canonical regardless of the row's ``image`` value.
    Empty-patch predictions (a sub-agent that overflowed before editing) are not graded and
    resolve to 0.
    """
    _pin_dataset_offline()
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
        model = predictions[graded[0]["instance_id"]].get("model_name_or_path", "")
        stale = stale_grader_report_paths(
            run_id, model, [instance["instance_id"] for instance in graded]
        )
        if stale:
            raise RuntimeError(
                f"refusing to grade: official report.json already exists for "
                f"{[path.parts[-2] for path in stale]} under run_id {run_id!r}; a v3 run must "
                f"not reuse a stale report. Move logs/run_evaluation/{run_id} aside and re-run "
                f"(audit B1)."
            )
        run_instances(
            predictions,
            graded,
            max_workers,
            run_id,
            1800,
            rewrite_reports=False,
            task_repo=None,
        )
    report_path = make_run_report(
        predictions, instances, run_id, _docker_client(), str(reports_dir)
    )
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    resolved: dict[str, int] = {instance_id: 0 for instance_id in instance_ids}
    for instance_id in report.get("resolved_ids", []):
        resolved[instance_id] = 1
    return resolved


# ------------------------------------------------------------------------------- receipt


def _token_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {key: after[key] - before[key] for key in before}


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file, skipping blank and malformed lines."""
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def append_run(path: Path, row: dict[str, Any]) -> None:
    """Append one finished/cap_stopped task x arm row to the persistent run receipt.

    Written as each run finishes so a mid-invocation crash cannot lose earlier rows
    (audit F12); the aggregator reads the latest row per pair.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def tooling_failures(path: Path) -> set[tuple[str, str]]:
    """The ``(instance_id, arm)`` pairs whose latest run row is ``tooling_failure``."""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _iter_jsonl(path):
        instance_id = row.get("instance_id")
        arm = row.get("arm")
        if isinstance(instance_id, str) and isinstance(arm, str):
            latest[(instance_id, arm)] = row
    return {pair for pair, row in latest.items() if row.get("exit_status") == "tooling_failure"}


def tooling_failure_reruns(path: Path) -> dict[tuple[str, str], int]:
    """Count of ``tooling_failure`` rows per ``(instance_id, arm)`` (audit N2).

    The single allowed re-run means a pair may carry at most one re-run attempt: the original
    failure plus the re-run is two ``tooling_failure`` rows, at which point a further
    ``--rerun-tooling-failures`` must be refused.
    """
    counts: dict[tuple[str, str], int] = {}
    for row in _iter_jsonl(path):
        if row.get("exit_status") != "tooling_failure":
            continue
        instance_id = row.get("instance_id")
        arm = row.get("arm")
        if isinstance(instance_id, str) and isinstance(arm, str):
            counts[(instance_id, arm)] = counts.get((instance_id, arm), 0) + 1
    return counts


def assert_single_tooling_rerun(path: Path) -> None:
    """Refuse a second ``--rerun-tooling-failures`` for a pair that already used its re-run."""
    over = sorted(pair for pair, count in tooling_failure_reruns(path).items() if count >= 2)
    if over:
        raise SystemExit(
            f"refusing a second tooling re-run for {over}: each pair already had its single "
            f"allowed re-run (audit N2)"
        )


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


def _arm_predictions_path(predictions_path: Path, arm: str) -> Path | None:
    """Write the per-arm predictions JSONL; ``None`` when there is nothing to grade for ``arm``.

    A missing predictions file (no task × arm finished yet, e.g. a fresh day stopped at the
    cap) is "nothing to grade", not an error.
    """
    if not predictions_path.is_file():
        return None
    per_arm = predictions_path.with_suffix(f".{arm}.jsonl")
    with (
        predictions_path.open(encoding="utf-8") as src,
        per_arm.open("w", encoding="utf-8") as dst,
    ):
        for line in src:
            row = json.loads(line)
            if row.get("arm") == arm:
                dst.write(line)
    if per_arm.stat().st_size == 0:
        return None
    return per_arm


def project_tokens(triplet_tokens: int, n: int, daily_cap: int) -> dict[str, Any]:
    """Project the full-run token load and the day count at ``daily_cap``.

    shortcut: only honest for a single triplet (``--limit 1``); a resumed/partial day
    multiplies the whole day's ledger by ``n`` and overstates the projection (audit L7).
    The live receipt no longer emits it; the aggregator computes real per-arm totals.
    """
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
        command += ["--obs-fold", "--obs-store", str(args.obs_store)]
    return command


def _sidecar_log_path(args: argparse.Namespace, arm: str) -> Path:
    return Path(args.reports_dir) / f"b8_sidecar_{arm}.log"


def _sidecar_env() -> dict[str, str]:
    """The sidecar's environment: the runner's env minus inherited ``CF_*`` defaults (audit N9).

    The runner sets every ``CF_*`` knob it wants explicitly on the command line; inheriting
    the rest would let a stray ``CF_OBS_FOLD`` / ``CF_FABRIC`` / ``CF_MODE`` silently change
    what arms A/B see.
    """
    return {key: value for key, value in os.environ.items() if not key.startswith("CF_")}


def _start_sidecar(args: argparse.Namespace, arm: str) -> subprocess.Popen[bytes]:
    # Fail fast if a stale sidecar already holds the port (audit F7): a health answer before
    # this process spawns means a killed invocation's sidecar is still bound.
    try:
        httpx.get(f"http://127.0.0.1:{args.port}/fabric/health", timeout=1.0)
        raise RuntimeError(
            f"port {args.port} already answers /fabric/health; a stale sidecar may hold it"
        )
    except httpx.HTTPError:
        pass
    log_path = _sidecar_log_path(args, arm)
    log = open(log_path, "ab")  # surface sidecar errors instead of /dev/null (audit F7)
    proc = subprocess.Popen(
        _sidecar_command(args, arm), stdout=log, stderr=subprocess.STDOUT, env=_sidecar_env()
    )
    for _ in range(200):
        if proc.poll() is not None:
            raise RuntimeError(f"cfx serve exited during startup (see {log_path})")
        try:
            httpx.get(f"http://127.0.0.1:{args.port}/fabric/health", timeout=1.0)
            return proc
        except httpx.HTTPError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError(f"cfx serve did not become healthy (see {log_path})")


def _sidecar_stats(port: int) -> dict[str, int]:
    """Snapshot of the spawned sidecar's ``/fabric/stats`` counters (audit F7)."""
    response = httpx.get(f"http://127.0.0.1:{port}/fabric/stats", timeout=5.0)
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    return {key: value for key, value in payload.items() if isinstance(value, int)}


def _stop_sidecar(proc: subprocess.Popen[bytes]) -> None:
    # Graceful stop first (SIGINT → serve() catches KeyboardInterrupt and checkpoints the
    # obs store); fall back to terminate/kill if it does not exit (audit AU5).
    try:
        proc.send_signal(signal.SIGINT)
    except (AttributeError, OSError, ValueError):
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
    parser.add_argument(
        "--artifact-root",
        default=str(Path(__file__).parent),
        help="directory under which ALL run artifacts (predictions, b8_runs.jsonl, parent "
        "cache, fold store, grader reports, sidecar logs, receipts) are written; the ledger "
        "stays shared (audit B2)",
    )
    parser.add_argument(
        "--parent-cache",
        default="",
        help="override the parent cache dir (default: <artifact-root>/parent_handoff_raw)",
    )
    parser.add_argument("--ledger-dir", default=str(LEDGER_DIR))
    parser.add_argument("--window-cap", type=int, default=WINDOW_CAP)
    parser.add_argument("--step-limit", type=int, default=STEP_LIMIT)
    parser.add_argument("--handoff-budget", type=int, default=HANDOFF_BUDGET)
    parser.add_argument("--daily-cap", type=int, default=b8.DEFAULT_DAILY_CAP)
    parser.add_argument(
        "--pair-reserve",
        type=int,
        default=RUN_RESERVE,
        help="refuse to start a task x arm unless today's remaining budget is at least this "
        "many tokens (audit F3)",
    )
    parser.add_argument("--out", default="")
    parser.add_argument("--resume", action="store_true", help="skip finished task x arm pairs")
    parser.add_argument(
        "--rerun-tooling-failures",
        action="store_true",
        help="re-run a task x arm previously recorded as tooling_failure (the single allowed "
        "tooling re-run, audit F12)",
    )
    parser.add_argument("--grade", action="store_true", help="grade predictions after running")
    parser.add_argument("--max-workers", type=int, default=2, help="grading worker count (Docker)")
    return parser.parse_args(argv)


def _require_model(model: str) -> None:
    if not b8.is_allowed_model(model):
        raise SystemExit(
            f"model {model!r} is not in the B8 allow-list {b8.ALLOWED_MODELS}; refusing to run"
        )


def pair_fits(used: int, pair_reserve: int, daily_cap: int) -> bool:
    """True when there is room for one more task x arm under the daily cap (audit F3)."""
    return used + pair_reserve <= daily_cap


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _require_model(args.model)
    if not args.repo_dir:
        raise SystemExit("--repo-dir (or CF_B8_REPO_DIR) is required: the extracted repo checkout")

    # audit B2: an --artifact-root isolates every run artifact from the v3 paths; the ledger
    # stays shared (it is the money cap). The defaults reproduce the historical paths.
    artifact_root = Path(args.artifact_root)
    args.reports_dir = artifact_root / "reports"
    args.parent_cache = (
        Path(args.parent_cache) if args.parent_cache else artifact_root / "parent_handoff_raw"
    )
    args.obs_store = args.reports_dir / "obs.db"
    predictions_path = args.reports_dir / "b8_predictions.jsonl"
    runs_jsonl = args.reports_dir / "b8_runs.jsonl"

    ledger = b8.Ledger(args.ledger_dir)
    instances = sample_instances(load_instances(), args.seed, args.n)
    assert_frozen_sample(instances, args.seed, args.n)
    selected = instances[: args.limit] if args.limit else instances
    print(f"b8_live: seed={args.seed} n={args.n} selected={len(selected)} model={args.model}")

    done = finished_pairs(predictions_path) if args.resume else set()
    if args.resume:
        if args.rerun_tooling_failures:
            # audit N2: a pair with two tooling_failure rows has already used its single
            # allowed re-run; refuse a second instead of silently re-running it again.
            assert_single_tooling_rerun(runs_jsonl)
        else:
            # The single allowed tooling re-run is opt-in (audit F12): without the flag a
            # previously tooling_failure pair is skipped, not silently re-run.
            done |= tooling_failures(runs_jsonl)

    base_url = args.base_url if args.no_sidecar else f"http://127.0.0.1:{args.port}/v1"
    parent_tokens: dict[str, int] = {}
    if not args.no_sidecar:
        # Phase 1: produce each task's frozen Parent hand-off once (one mini-tier turn).
        parent_proc = _start_sidecar(args, "A")
        day = b8.utc_day()
        before = ledger.summary(day)
        for instance in selected:
            repo_root = Path(args.repo_dir) / instance["instance_id"]
            if repo_root.is_dir():
                result = parent_handoff(
                    instance,
                    repo_root,
                    model=args.model,
                    base_url=base_url,
                    cache_dir=args.parent_cache,
                )
                if result.get("parent_status") == "cap_stopped":
                    print("b8_live: parent phase hit the daily cap; stopping for the day.")
                    break
        parent_tokens = _token_delta(before, ledger.summary(day))
        _stop_sidecar(parent_proc)

    day_stopped = False
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
                day = b8.utc_day()  # audit F10: per pair, so midnight crossings read the right day
                # audit F3: reserve a realistic per-run maximum before starting a pair, so a
                # run is never cut off mid-flight (stop cleanly for the day instead).
                if not pair_fits(ledger.day_total(day), args.pair_reserve, args.daily_cap):
                    print(
                        f"b8_live: remaining budget < --pair-reserve ({args.pair_reserve}); "
                        f"stopping cleanly for the day."
                    )
                    day_stopped = True
                    break
                try:
                    parent = parent_handoff(
                        instance,
                        repo_root,
                        model=args.model,
                        base_url=base_url,
                        cache_dir=args.parent_cache,
                    )
                    if parent.get("parent_status") == "cap_stopped":
                        print(
                            f"b8_live: {instance_id} parent hit the daily cap; "
                            "stopping for the day."
                        )
                        day_stopped = True
                        break
                    before = ledger.summary(day)
                    if parent.get("parent_status") == "llm_retry":
                        # audit N3: a transient parent transport fault is not cached and not a
                        # result; the task retries on a later invocation.
                        print(
                            f"b8_live: {instance_id} parent transient failure"
                            f" ({parent.get('error', '')}); retrying on a later invocation."
                        )
                        continue
                    if parent.get("parent_status") != "llm_ok":
                        row = {
                            "exit_status": "parent_failed",
                            "overflow": False,
                            "steps": 0,
                            "wall_time_s": 0.0,
                            "tokens": _token_delta(before, before),
                            "parent_error": parent.get("error", ""),
                        }
                        arm_receipts[arm][instance_id] = row
                        append_run(runs_jsonl, _run_row(instance_id, arm, row, args.model, day, 0))
                        print(
                            f"b8_live: {instance_id} parent llm_failed"
                            f" ({parent.get('error', '')}); skipping arm {arm}"
                        )
                        continue
                    # audit F1: every arm gets the identical task header (problem + query + notes)
                    # before its own hand-off; the header is not counted in the 8,192-token slot.
                    header = build_task_header(
                        instance["problem_statement"],
                        parent["subagent_query"],
                        parent["parent_notes"],
                    )
                    header_tokens = count_tokens(header)
                    objective = " ".join(
                        part
                        for part in (
                            instance["problem_statement"],
                            parent.get("subagent_query", ""),
                        )
                        if part
                    )
                    handoff = build_handoff(
                        arm,
                        repo_root,
                        parent["candidate_files"],
                        objective,
                        project_id=instance["repo"].split("/")[-1],
                        budget=args.handoff_budget,
                    )
                    task_text = build_subagent_task(header, handoff)
                    output_path = (
                        Path(args.repo_dir) / instance_id / f"{instance_id}.{arm}.traj.json"
                    )
                    fold_before = (
                        _sidecar_stats(args.port) if arm == "C" and proc is not None else {}
                    )
                except Exception as exc:
                    # audit N2: a fault building the hand-off / reading sidecar stats is a
                    # tooling failure, recorded like a run, not a crash of the whole invocation.
                    row = {
                        "exit_status": "tooling_failure",
                        "overflow": False,
                        "steps": 0,
                        "wall_time_s": 0.0,
                        "tokens": _token_delta(ledger.summary(day), ledger.summary(day)),
                        "patch_source": "",
                        "sidecar_error": f"{type(exc).__name__}: {exc}",
                        "fold_events": 0,
                        "folded_bytes": 0,
                    }
                    arm_receipts[arm][instance_id] = row
                    append_run(runs_jsonl, _run_row(instance_id, arm, row, args.model, day, 0))
                    print(
                        f"b8_live: {instance_id} arm {arm} -> tooling_failure building hand-off "
                        f"({row['sidecar_error']})."
                    )
                    continue
                receipt = run_subagent(
                    instance,
                    task_text,
                    model=args.model,
                    base_url=base_url,
                    output_path=output_path,
                    step_limit=args.step_limit,
                )
                tokens = _token_delta(before, ledger.summary(day))
                fold_events = folded_bytes = 0
                if arm == "C" and proc is not None:
                    fold_after = _sidecar_stats(args.port)
                    fold_events = fold_after.get("folded_events", 0) - fold_before.get(
                        "folded_events", 0
                    )
                    folded_bytes = fold_after.get("folded_bytes", 0) - fold_before.get(
                        "folded_bytes", 0
                    )
                row = {
                    "exit_status": receipt["exit_status"],
                    "overflow": receipt["exit_status"] == "ContextWindowExceededError",
                    "steps": receipt["steps"],
                    "wall_time_s": receipt["wall_time_s"],
                    "tokens": tokens,
                    "patch_source": receipt["patch_source"],
                    "sidecar_error": receipt["sidecar_error"],
                    "fold_events": fold_events,
                    "folded_bytes": folded_bytes,
                }
                arm_receipts[arm][instance_id] = row
                append_run(
                    runs_jsonl, _run_row(instance_id, arm, row, args.model, day, header_tokens)
                )
                if receipt["exit_status"] == "cap_stopped":
                    print(
                        f"b8_live: {instance_id} arm {arm} -> cap_stopped (daily cap). "
                        "Resume tomorrow."
                    )
                    day_stopped = True
                    break
                if receipt["exit_status"] == "tooling_failure":
                    print(
                        f"b8_live: {instance_id} arm {arm} -> tooling_failure"
                        f" ({receipt['sidecar_error']}); not written to predictions "
                        "(re-run with --rerun-tooling-failures)."
                    )
                    continue
                write_prediction(
                    predictions_path, instance_id, arm, args.model, receipt["submission"]
                )
                print(
                    f"b8_live: {instance_id} arm {arm} -> {receipt['exit_status']} "
                    f"steps={receipt['steps']} tokens={tokens['total_tokens']} "
                    f"patch={receipt['patch_source'] or 'none'}"
                )
        finally:
            if proc is not None:
                _stop_sidecar(proc)
        if day_stopped:
            break

    resolved: dict[str, dict[str, int]] = {arm: {} for arm in ARMS}
    if args.grade:
        if not predictions_path.is_file():
            print("b8_live: nothing to grade (no predictions written).")
        else:
            for arm in ARMS:
                if not arm_receipts[arm]:
                    continue
                per_arm_preds = _arm_predictions_path(predictions_path, arm)
                if per_arm_preds is None:
                    print(f"b8_live: nothing to grade for arm {arm}.")
                    continue
                resolved[arm] = grade(
                    per_arm_preds, run_id=grader_run_id(arm), reports_dir=args.reports_dir
                )
                for instance_id, value in resolved[arm].items():
                    arm_receipts[arm].setdefault(instance_id, {})["resolved"] = value

    receipt = {
        "scenario": "b8_live",
        "kind": "smoke" if args.limit else "live",
        "seed": args.seed,
        "n": args.n,
        "model": args.model,
        "window_cap": args.window_cap,
        "handoff_budget": args.handoff_budget,
        "pair_reserve": args.pair_reserve,
        "step_limit": args.step_limit,
        "started_at": datetime.now(tz=UTC).isoformat(),
        "harness_sha": harness_sha(),
        "day": b8.utc_day(),
        "instance_ids": [i["instance_id"] for i in selected],
        "arms": arm_receipts,
        "resolved": resolved,
        "parent_tokens": parent_tokens,
        "ledger": ledger.summary(b8.utc_day()),
    }
    ts = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) if args.out else args.reports_dir / f"b8_live_{ts}.json"
    out.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    _print_report(receipt, out)
    return 0


def _run_row(
    instance_id: str,
    arm: str,
    row: dict[str, Any],
    model: str,
    day: str,
    header_tokens: int,
) -> dict[str, Any]:
    """The persistent JSONL run row: one per finished/cap_stopped/failed task x arm."""
    return {
        "instance_id": instance_id,
        "arm": arm,
        "exit_status": row["exit_status"],
        "overflow": row["overflow"],
        "steps": row["steps"],
        "wall_time_s": row["wall_time_s"],
        "tokens": row["tokens"],
        "header_tokens": header_tokens,
        "patch_source": row.get("patch_source", ""),
        "sidecar_error": row.get("sidecar_error", ""),
        "fold_events": row.get("fold_events", 0),
        "folded_bytes": row.get("folded_bytes", 0),
        "model": model,
        "day": day,
        "harness_sha": harness_sha(),
        "ts": datetime.now(tz=UTC).isoformat(),
    }


def _print_report(receipt: dict[str, Any], out: Path) -> None:
    print(f"\nb8_live report ({out}):")
    for arm in ARMS:
        rows = list(receipt["arms"].get(arm, {}).values())
        # Rows without tokens (earlier-day resolved-only entries) and non-results are excluded
        # from the token totals (audit F4/F6/N10): a tooling_failure is not a result.
        excluded_statuses = ("cap_stopped", "parent_failed", "tooling_failure")
        finished = [
            r for r in rows if "tokens" in r and r.get("exit_status") not in excluded_statuses
        ]
        excluded = sum(1 for r in rows if r.get("exit_status") in excluded_statuses)
        total = sum(r["tokens"]["total_tokens"] for r in finished)
        cached = sum(r["tokens"]["cached_tokens"] for r in finished)
        uncached = sum(r["tokens"]["uncached_tokens"] for r in finished)
        overflows = sum(1 for r in finished if r["overflow"])
        resolved = sum(1 for r in finished if r.get("resolved") == 1)
        steps = [r["steps"] for r in finished]
        wall = [r["wall_time_s"] for r in finished]
        print(
            f"  arm {arm}: tasks={len(finished)} resolved={resolved} overflows={overflows} "
            f"billed={total} cached={cached} uncached={uncached} "
            f"steps={steps} wall_s={wall} excluded={excluded}"
        )
    ledger = receipt["ledger"]
    print(
        f"  ledger total (day {receipt['day']}): {ledger['total_tokens']} "
        f"(prompt {ledger['prompt_tokens']}, completion {ledger['completion_tokens']})"
    )


if __name__ == "__main__":
    raise SystemExit(main())
