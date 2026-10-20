"""Terminal-Bench 2.1 adapter — pure, offline-testable wiring (no network, no Docker).

Mirrors ``evals/b8_live.py`` (frozen) but targets the Harbor harness for
Terminal-Bench 2.1. The sub-agent is Harbor's pre-integrated ``mini-swe-agent``, whose
model calls route through ``cfx serve`` via ``--base-url`` (window cap + ledger + optional
``--obs-fold``) — the exact same choke point B8 uses. The three arms map 1:1 from B8:

* **Arm A** — naive raw file dump (FIFO-truncated at the 8,192-token hand-off slot), no fold;
* **Arm B** — current CF packet (``compile_packet``, 4-tier), no fold;
* **Arm C** — BM25 line-window skim + shared repo map (cartridge), plus ``--obs-fold``.

Only the pure, deterministic pieces live here (sampling, hand-off builders, the ``cfx
serve`` and ``harbor run`` argv builders). The live driver that spawns ``cfx serve`` and
runs ``harbor run`` inside Docker is documented in ``docs/engineering/TBENCH-DESIGN.md``
and is not yet implemented (it needs the WSL2 + Docker environment and the Harbor install).

B8's ``build_blocks`` / ``build_ast_chunks`` are Python-only (stdlib ``ast``), so the B/C
hand-off builders here are language-agnostic: Python files get symbol facts via
``extract_source``, everything else degrades to a raw full-file fact (arm B) or line-window
chunks (arm C). A tree-sitter backend (already anticipated in
``kernel/cartridge/ast_extractor.py``) is the post-B8 upgrade for true polyglot body
stripping; see the design doc's risks.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evals.b8_live import build_task_header as build_task_header
from evals.b8_live import handoff_arm_a, sample_instances
from kernel.cartridge.ast_extractor import extract_source
from kernel.cartridge.serializer import Cartridge, build_cartridge, count_tokens
from kernel.router.compiler import DEFAULT_AS_OF, compile_packet
from kernel.router.recall import bm25_scores
from protocol.canonical import sha256_hex
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
from sidecar.render import render_packet

# --- harness / task constants ----------------------------------------------------------

# Harbor Hub dataset id (the official TB 2.1 distribution; 89 tasks, 0 unsolved).
DATASET = "terminal-bench/terminal-bench-2-1"
# Harbor's pre-integrated mini-swe-agent: the same scaffold B8 drives directly.
AGENT = "mini-swe-agent"
ARMS = ("A", "B", "C")

# Same artificial small-window cap and hand-off slot as B8 (pre-registered there).
WINDOW_CAP = 24_576
HANDOFF_BUDGET = 8_192

DEFAULT_SEED = 20261019
# The pilot mirrors B8's default n=10; the full run is the ReFold-aligned n=50 subset.
DEFAULT_N = 10

# The sidecar's upstream choke point (same as B8). Never printed/logged with a key.
UPSTREAM = "https://api.openai.com"

# Container -> host networking: Docker Desktop injects host.docker.internal on WSL2, so the
# agent container reaches the host-run ``cfx serve`` at this address.
HOST_BASE_URL = "http://host.docker.internal:8080/v1"

SCOPE = Scope(
    tenant_id="default",
    project_id="tbench",
    workflow_id="terminal-bench-2-1",
    task_id="t1",
)
RECIPIENT = Recipient(agent_id="subagent-solver", role="writer", stage="solve")

# A Terminal-Bench 2.1 task row (from the Harbor dataset) must carry at least these keys;
# the live loader fills the rest (docker_image, agent_timeout_sec, memory_mb, ...).
REQUIRED_TASK_KEYS = ("task_id", "instruction", "category")

_CREATOR = Creator(type=CreatorType.tool, id="tbench")
_AS_OF = DEFAULT_AS_OF


# --- sampling -------------------------------------------------------------------------


def sample_tasks(
    tasks: Sequence[dict[str, Any]], seed: int = DEFAULT_SEED, n: int = DEFAULT_N
) -> list[dict[str, Any]]:
    """Deterministic category-stratified sample (Hamilton largest-remainder, floor >= 1).

    Terminal-Bench 2.1 has no ``repo`` dimension; tasks are grouped by ``category``
    (system-administration, software-engineering, machine-learning, security, ...). This
    reuses B8's frozen ``sample_instances`` verbatim by relabelling ``task_id`` ->
    ``instance_id`` and ``category`` -> ``repo``, then maps the draw back to the original
    rows. Sorted by ``task_id``.
    """
    if not tasks:
        return []
    relabelled = [
        {**task, "instance_id": task["task_id"], "repo": task["category"]} for task in tasks
    ]
    sampled = sample_instances(relabelled, seed, n)
    by_id = {task["task_id"]: task for task in tasks}
    return [by_id[instance["instance_id"]] for instance in sampled]


# --- hand-off builders (language-agnostic twins of B8's arms) -------------------------


def _fact_block(
    block_id: str, content: str, seq: int, rel: str | None, exported: tuple[str, ...]
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
        creator=_CREATOR,
        fidelity=Fidelity.extract,
        trust_domain="default",
        symbols_exported=exported,
        observed_at=_AS_OF,
        valid_from=_AS_OF,
    )


def _repo_cartridge(repo_root: Path, files: Sequence[str]) -> Cartridge:
    """The shared Tier-0 repo map (hologram) over ``files`` under ``repo_root``.

    Non-Python files contribute ``(no exports)`` to the hologram (``extract_source``
    ignores them) but still appear as a path entry, so the map stays complete.
    """
    symbols = []
    hashes = []
    for rel in files:
        text = (repo_root / rel).read_text(encoding="utf-8", errors="replace")
        symbols.append(extract_source(rel, text))
        hashes.append(f"{rel} {sha256_hex((repo_root / rel).read_bytes())}")
    commit = sha256_hex("\n".join(hashes).encode("utf-8"))
    return build_cartridge(symbols, tenant_id="default", project_id="tbench", commit_hash=commit)


def _polyglot_blocks(repo_root: Path, files: Sequence[str]) -> list[ContextBlock]:
    """Arm-B blocks: Python symbol facts; raw full-file facts for every other language.

    B8's ``build_blocks`` is Python-only. This is the honest current-CF behaviour on a
    polyglot Terminal-Bench container: body stripping only exists for Python; C/Rust/
    Makefile/TeX files degrade to a raw fact the 4-tier budget then trims.
    """
    blocks: list[ContextBlock] = []
    seq = 0
    for rel in files:
        text = (repo_root / rel).read_text(encoding="utf-8", errors="replace")
        if rel.endswith(".py"):
            for sym in extract_source(rel, text).exported:
                seq += 1
                blocks.append(
                    _fact_block(
                        f"blk_{sha256_hex(f'{rel}:{sym.name}'.encode())[:16]}",
                        f"{rel}: {sym.signature}",
                        seq,
                        rel,
                        (sym.name,),
                    )
                )
        else:
            seq += 1
            blocks.append(
                _fact_block(
                    f"blk_{sha256_hex(rel.encode())[:16]}",
                    f"===== FILE: {rel} =====\n{text}",
                    seq,
                    rel,
                    (),
                )
            )
    return blocks


def handoff_arm_b(
    repo_root: Path,
    candidate_files: Sequence[str],
    objective: str,
    *,
    budget: int = HANDOFF_BUDGET,
) -> str:
    """Current CF packet: ``compile_packet`` (4-tier) over polyglot blocks + shared map."""
    blocks = _polyglot_blocks(repo_root, list(candidate_files))
    result = compile_packet(
        scope=SCOPE,
        recipient=RECIPIENT,
        objective=objective,
        stage="solve",
        blocks=blocks,
        cartridge=_repo_cartridge(repo_root, candidate_files),
        budget_base=budget - budget % 20,
        as_of=_AS_OF,
    )
    return render_packet(result.packet)


def _line_window_chunks(
    repo_root: Path, files: Sequence[str], window: int = 40, overlap: int = 10
) -> list[ContextBlock]:
    """Arm-C chunks: language-agnostic line windows with exact line numbers.

    The polyglot twin of B8's ``build_ast_chunks`` (question-guided skim). Each chunk is a
    ``window``-line slice with a 1-based ``L<start>-L<end>`` header so the sub-agent can
    edit immediately without re-reading the file.
    """
    chunks: list[ContextBlock] = []
    seq = 0
    for rel in files:
        lines = (repo_root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        start = 0
        while start < len(lines):
            end = min(start + window, len(lines))
            content = f"===== FILE: {rel} (L{start + 1}-L{end}) =====\n" + "\n".join(
                lines[start:end]
            )
            seq += 1
            chunks.append(_fact_block(f"chunk_{rel}:{start + 1}", content, seq, rel, ()))
            if end >= len(lines):
                break
            start += window - overlap
    return chunks


def handoff_arm_c(
    repo_root: Path,
    candidate_files: Sequence[str],
    objective: str,
    *,
    budget: int = HANDOFF_BUDGET,
) -> str:
    """BM25 line-window skim + shared repo map (the rx_hybrid R/H2 synthesis, polyglot)."""
    chunks = _line_window_chunks(repo_root, list(candidate_files))
    scores = bm25_scores(objective, chunks)
    ranked = sorted(chunks, key=lambda c: (-scores.get(c.block_id, 0.0), c.block_id))
    parts = [_repo_cartridge(repo_root, candidate_files).hologram_markdown]
    spent = count_tokens(parts[0])
    separator = count_tokens("\n\n") + 1
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
    budget: int = HANDOFF_BUDGET,
) -> str:
    """Dispatch an arm to its hand-off builder (A is B8's frozen raw-dump, verbatim)."""
    if arm == "A":
        return handoff_arm_a(repo_root, candidate_files, budget=budget)
    if arm == "B":
        return handoff_arm_b(repo_root, candidate_files, objective, budget=budget)
    if arm == "C":
        return handoff_arm_c(repo_root, candidate_files, objective, budget=budget)
    raise ValueError(f"unknown arm {arm!r}")


# --- harness / sidecar argv builders --------------------------------------------------


def sidecar_command(
    port: int,
    arm: str,
    *,
    window_cap: int = WINDOW_CAP,
    ledger_dir: str,
    daily_cap: int,
    obs_store: str = "",
) -> list[str]:
    """The exact ``cfx serve`` argv for an arm (mirrors ``b8_live._sidecar_command``).

    ``--obs-fold`` (plus its store) is on only for Arm C; A/B run byte-identical passthrough
    under the same window cap and daily ledger.
    """
    command = [
        sys.executable,
        "-m",
        "sidecar",
        "serve",
        "--upstream",
        UPSTREAM,
        "--mode",
        "passthrough",
        "--port",
        str(port),
        "--window-cap",
        str(window_cap),
        "--b8-ledger-dir",
        ledger_dir,
        "--b8-daily-cap",
        str(daily_cap),
    ]
    if arm == "C":
        command += ["--obs-fold", "--obs-store", obs_store]
    return command


def harbor_command(
    model: str,
    *,
    base_url: str = HOST_BASE_URL,
    dataset: str = DATASET,
    agent: str = AGENT,
    n_concurrent: int = 4,
    n_attempts: int = 1,
    env: str = "docker",
) -> list[str]:
    """The exact ``harbor run`` argv for an arm (Harbor v0.24.0).

    v0.24.0 resolves the model endpoint/credential from environment variables, not
    ``--base-url`` (a newer-Harbor flag that 0.24.0 lacks). mini-swe-agent reads
    ``OPENAI_BASE_URL`` / ``OPENAI_API_BASE`` for the endpoint and ``MSWEA_API_KEY``
    (falling back to ``OPENAI_API_KEY`` for the ``openai/`` provider) for the key, so the
    endpoint is pinned via ``--agent-env`` and the key is passed at call time, never here.
    The ``openai/`` model prefix keeps mini-swe-agent on its chat-completions ``litellm``
    path (the same non-streaming path B8's ledger binds to); passing ``reasoning_effort``
    would switch openai models to the Responses API.
    """
    return [
        "harbor",
        "run",
        "-d",
        dataset,
        "-a",
        agent,
        "-m",
        f"openai/{model}",
        "-e",
        env,
        "-n",
        str(n_concurrent),
        "-k",
        str(n_attempts),
        "--ae",
        f"OPENAI_BASE_URL={base_url}",
    ]
