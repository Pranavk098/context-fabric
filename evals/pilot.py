"""Pre-registered pilot — raw repository vs compiled packet on real httpx bytes.

Design, objectives, keys, arms and success criteria are frozen in
``docs/engineering/PILOT-PREREGISTRATION.md`` before any run. One fixture (the
``encode/httpx`` clone at the preregistered commit), one bundle of actual file bytes
(raw arm system message, >=30k tiktoken per request), one 11-block fact fabric (compile
arm), six role objectives, one scorer for both arms, one GPU session.

Run (WSL, from the repo root, vLLM already serving):

    ~/.venvs/vllm/bin/python -m evals.pilot --fixture <httpx clone> [--smoke]
        [--upstream http://127.0.0.1:8000] [--model Qwen/Qwen2.5-1.5B-Instruct]

Artifacts: ``evals/reports/pilot[_smoke]_<ts>.json``,
``evals/reports/pilot_cartridge_<ts>.json``,
``evals/fixtures/pilot_fixture_<ts>.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from evals.mech_fabric import build_blocks
from kernel.cartridge.ast_extractor import extract_tree
from kernel.cartridge.serializer import Cartridge, build_cartridge, count_tokens
from kernel.router.compiler import DEFAULT_AS_OF, compile_packet
from kernel.store import SqliteStore
from protocol.canonical import content_hash, sha256_hex
from protocol.models import (
    BlockKind,
    ContextBlock,
    Creator,
    CreatorType,
    Fidelity,
    LifecycleStatus,
    MemoryTier,
    Recipient,
    Scope,
)
from sidecar.demo import (
    _Completion,
    _Counters,
    _delta,
    _scrape_cache,
    _serve,
    _start_kv_sampler,
    _stop,
    _totals,
    score_answer,
)
from sidecar.render import render_packet
from sidecar.server import make_server, open_store

REPORTS = Path(__file__).parent / "reports"
FIXTURES = Path(__file__).parent / "fixtures"

PREGISTERED_COMMIT = "b5addb64f0161ff6bfe94c124ef76f6a1fba5254"
PREGISTERED_REPO = "https://github.com/encode/httpx"

# The fixture bundle: the first 10 files of `httpx/**/*.py` in sorted relative-path order
# (smallest sorted prefix >= 30k tiktoken; see the preregistration, section 2).
BUNDLE_FILES: tuple[str, ...] = (
    "httpx/__init__.py",
    "httpx/__version__.py",
    "httpx/_api.py",
    "httpx/_auth.py",
    "httpx/_client.py",
    "httpx/_config.py",
    "httpx/_content.py",
    "httpx/_decoders.py",
    "httpx/_exceptions.py",
    "httpx/_main.py",
)

_RENDERING = (
    "files in listed order, raw bytes, joined by a blank line, each prefixed by "
    "'===== FILE: <path> ====='"
)

# The sidecar's compile scope (mirrors sidecar/server.py `_SCOPE`): the compile arm
# compiles packets with this fixed scope and no cartridge, so the offline dry-run
# replicates the live path exactly.
_SCOPE = Scope(tenant_id="default", project_id="default", workflow_id="chat", task_id="chat")

# 11 hand-derived fact blocks grounded in the bundle's real content (preregistration
# section 4); source file of each block is named in its id.
_FACT_BLOCKS: tuple[tuple[str, str], ...] = (
    (
        "blk_httpx_init",
        "Module map: httpx/__init__.py re-exports the package public API: from ._api import *,"
        " from ._auth import *, from ._client import *, from ._config import *,"
        " from ._content import *, from ._exceptions import *, from ._main import main."
        " It also re-exports __version__ from httpx/__version__.py.",
    ),
    (
        "blk_httpx_version",
        "httpx/__version__.py defines the package metadata: __title__ = 'httpx',"
        " __description__ = 'A next generation HTTP client, for Python 3.',"
        " __version__ = '0.28.1'.",
    ),
    (
        "blk_httpx_api",
        "httpx/_api.py defines the top-level request helpers: request(), stream(), get(),"
        " options(), head(), post(), put(), patch(), delete(). Each verb helper delegates to"
        " request() with its method, e.g. get() returns request('GET', url, ...). request()"
        " defaults: timeout=DEFAULT_TIMEOUT_CONFIG, follow_redirects=False, trust_env=True.",
    ),
    (
        "blk_httpx_auth",
        "httpx/_auth.py defines the authentication classes: Auth (base), FunctionAuth,"
        " BasicAuth, NetRCAuth, DigestAuth. BasicAuth._build_auth_header base64-encodes"
        " 'username:password' and prefixes it with 'Basic '; the header sets Authorization.",
    ),
    (
        "blk_httpx_client_classes",
        "httpx/_client.py defines class BaseClient and its subclasses class Client(BaseClient)"
        " and class AsyncClient(BaseClient); __all__ = ['USE_CLIENT_DEFAULT', 'AsyncClient',"
        " 'Client']. Client is the synchronous client; AsyncClient provides the async API.",
    ),
    (
        "blk_httpx_client_redirects",
        "httpx/_client.py redirect handling: it raises TooManyRedirects (message 'Exceeded"
        " maximum allowed redirects.') when len(history) > self.max_redirects; the BaseClient"
        " default is max_redirects=DEFAULT_MAX_REDIRECTS (20), limits=DEFAULT_LIMITS,"
        " timeout=DEFAULT_TIMEOUT_CONFIG. Client.request() takes auth, follow_redirects and"
        " timeout with the USE_CLIENT_DEFAULT sentinel.",
    ),
    (
        "blk_httpx_config",
        "httpx/_config.py defines class Timeout (fields connect, read, write, pool; docstring"
        " example Timeout(5.0)), class Limits (max_connections, max_keepalive_connections,"
        " keepalive_expiry), class Proxy, create_ssl_context, and the module defaults:"
        " DEFAULT_TIMEOUT_CONFIG = Timeout(timeout=5.0); DEFAULT_LIMITS ="
        " Limits(max_connections=100, max_keepalive_connections=20); DEFAULT_MAX_REDIRECTS ="
        " 20.",
    ),
    (
        "blk_httpx_content",
        "httpx/_content.py builds request and response bodies: ByteStream,"
        " IteratorByteStream, AsyncIteratorByteStream, UnattachedStream, encode_content,"
        " encode_urlencoded_data, encode_multipart_data, encode_text, encode_html,"
        " encode_json, encode_request, encode_response.",
    ),
    (
        "blk_httpx_decoders",
        "httpx/_decoders.py handles Content-Encoding response bodies: class ContentDecoder"
        " and subclasses IdentityDecoder, DeflateDecoder, GZipDecoder (gzip), BrotliDecoder"
        " (br), ZStandardDecoder (zstd), MultiDecoder; plus ByteChunker, TextChunker,"
        " TextDecoder, LineDecoder. SUPPORTED_DECODERS maps 'identity', 'gzip' ->"
        " GZipDecoder, 'deflate', 'br', 'zstd'.",
    ),
    (
        "blk_httpx_exceptions",
        "httpx/_exceptions.py defines the exception hierarchy: HTTPError > RequestError >"
        " TransportError > TimeoutException (ConnectTimeout, ReadTimeout, WriteTimeout,"
        " PoolTimeout), NetworkError (ConnectError, ReadError, WriteError, CloseError),"
        " ProtocolError, ProxyError, UnsupportedProtocol; RequestError also parents"
        " DecodingError and TooManyRedirects; HTTPStatusError(HTTPError), InvalidURL,"
        " CookieConflict, StreamError variants.",
    ),
    (
        "blk_httpx_main",
        "httpx/_main.py implements the httpx command-line interface (CLI): the click command"
        " @click.command(add_help_option=False) def main(...), which renders help with rich"
        " and prints the response status, headers, and body. The console entry point in"
        " pyproject.toml is httpx = 'httpx:_main:main'.",
    ),
)


class PilotObjective(NamedTuple):
    role: str
    objective: str
    keys: tuple[str, ...]


# role, objective, required-substring key(s) — preregistration section 3 (frozen).
_PILOT_OBJECTIVES: tuple[PilotObjective, ...] = (
    PilotObjective(
        "architect",
        "In the httpx repository, which module defines the `Client` and `AsyncClient` "
        "classes? Answer with the file path only.",
        ("_client.py",),
    ),
    PilotObjective(
        "implementer",
        "In httpx, which function do the top-level helpers `get()` and `post()` delegate to? "
        "Answer with the function name only.",
        ("request",),
    ),
    PilotObjective(
        "reviewer",
        "In httpx, which exception is raised when a request exceeds the maximum number of "
        "redirects? Answer with the exception class name only.",
        ("toomanyredirects",),
    ),
    PilotObjective(
        "tester",
        "In httpx/_config.py, what is the default maximum number of connections given by "
        "`DEFAULT_LIMITS`? Answer with the number only.",
        ("100",),
    ),
    PilotObjective(
        "auditor",
        "Which class in httpx/_decoders.py decodes gzip (`content-encoding: gzip`) response "
        "bodies? Answer with the class name only.",
        ("gzipdecoder",),
    ),
    PilotObjective(
        "ops",
        "Which module in the httpx package implements the command-line interface (the "
        "`httpx` CLI)? Answer with the file path only.",
        ("_main.py",),
    ),
)


def pilot_workload() -> tuple[PilotObjective, ...]:
    """Six roles; each objective is answerable from the preregistered source lines."""
    return _PILOT_OBJECTIVES


def render_bundle(root: Path) -> tuple[str, list[dict[str, Any]]]:
    """The raw arm's system message: actual bytes, stable order, `FILE:` headers."""
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


def verify_keys(
    rendered: str, specs: Sequence[PilotObjective], blocks: Sequence[tuple[str, str]]
) -> None:
    """Construction check: every key must exist in the raw bundle and in the fabric."""
    bundle = rendered.lower()
    fabric = "\n".join(content for _, content in blocks).lower()
    missing_raw = sorted({key for spec in specs for key in spec.keys if key.lower() not in bundle})
    missing_fabric = sorted(
        {key for spec in specs for key in spec.keys if key.lower() not in fabric}
    )
    if missing_raw or missing_fabric:
        raise ValueError(
            f"answer keys missing from raw bundle: {missing_raw}; from fact blocks: "
            f"{missing_fabric} (fixture/content error, run aborted before any request)"
        )


def verify_raw_answerable(rendered: str, specs: Sequence[PilotObjective]) -> None:
    """Construction check (mech mode): every key must be present in the raw bundle only.

    The mechanical fabric never reads objectives or answer keys, so whether a key reaches
    the compiled packet is an outcome (reported by the dry-run recall table), never a
    precondition.
    """
    bundle = rendered.lower()
    missing_raw = sorted({key for spec in specs for key in spec.keys if key.lower() not in bundle})
    if missing_raw:
        raise ValueError(
            f"answer keys missing from raw bundle: {missing_raw} "
            "(fixture/content error, run aborted before any request)"
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


def compile_cartridge(root: Path, commit: str) -> Cartridge:
    """The real Tier-0 compiler (AST extractor + PageRank + serializer) on the fixture."""
    files = extract_tree(root / "httpx")
    return build_cartridge(
        files,
        tenant_id="default",
        project_id="httpx-pilot",
        commit_hash=commit,
        budget_tokens=600,
    )


def _hand_blocks() -> list[ContextBlock]:
    """The 11 frozen hand-derived fact blocks (preregistration section 4)."""
    as_of = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        ContextBlock(
            block_id=block_id,
            block_kind=BlockKind.fact,
            memory_tier=MemoryTier.ledger,
            content=content,
            content_hash="sha256:" + sha256_hex(content.encode("utf-8")),
            token_estimate=count_tokens(content),
            source_event_seq=seq,
            creator=Creator(type=CreatorType.tool, id="pilot-fixture"),
            fidelity=Fidelity.extract,
            lifecycle_status=LifecycleStatus.routable,
            trust_domain="default",
            observed_at=as_of,
            valid_from=as_of,
        )
        for seq, (block_id, content) in enumerate(_FACT_BLOCKS, start=1)
    ]


def build_pilot_store(path: Path) -> Path:
    """Append the 11 fact blocks through the normal store path (routable, trusted)."""
    store = SqliteStore(path)
    for block in _hand_blocks():
        store.append(block)
    return path


def build_mech_store(path: Path, fixture: Path) -> Path:
    """Append the mechanical fabric (mech_fabric.build_blocks) through the normal path."""
    store = SqliteStore(path)
    for block in build_blocks(fixture, BUNDLE_FILES):
        store.append(block)
    return path


def pilot_blocks(fixture: Path, fabric: str) -> tuple[ContextBlock, ...]:
    """The compile arm's fabric blocks for ``fabric`` (hand = frozen, mech = mechanical)."""
    if fabric == "mech":
        return tuple(build_blocks(fixture, BUNDLE_FILES))
    return tuple(_hand_blocks())


def _compile_packet_offline(
    blocks: Sequence[ContextBlock], spec: PilotObjective, as_of: datetime
) -> str:
    """Replicate the sidecar's compile path (budget_base=4000, no cartridge) client-side."""
    result = compile_packet(
        scope=_SCOPE,
        recipient=Recipient(agent_id=f"pilot-{spec.role}", role=spec.role, stage="chat"),
        objective=spec.objective,
        stage="chat",
        blocks=blocks,
        watermark_event_seq=0,
        budget_base=4000,
        as_of=as_of,
    )
    return render_packet(result.packet)


def dry_run(fixture: Path, *, fabric: str, as_of: datetime = DEFAULT_AS_OF) -> dict[str, Any]:
    """Offline build of both arms (no network, no model): token counts + key recall.

    The raw arm is the rendered bundle + objective; the compile arm is the sidecar's
    compile path (budget_base=4000, no cartridge) replicated client-side. Per-objective
    recall of each key in the compiled packet is an OUTCOME, never a gate.
    """
    rendered, files = render_bundle(fixture)
    specs = pilot_workload()
    verify_raw_answerable(rendered, specs)
    commit, commit_source = commit_of(fixture)
    blocks = pilot_blocks(fixture, fabric)
    raw_rows: list[dict[str, Any]] = []
    compile_rows: list[dict[str, Any]] = []
    recall_rows: list[dict[str, Any]] = []
    raw_total = compile_total = 0
    for spec in specs:
        raw_prompt = count_tokens(rendered) + count_tokens(spec.objective)
        raw_total += raw_prompt
        raw_rows.append(
            {"role": spec.role, "objective": spec.objective, "prompt_tokens": raw_prompt}
        )
        compiled = _compile_packet_offline(blocks, spec, as_of)
        compile_prompt = count_tokens(compiled) + count_tokens(spec.objective)
        compile_total += compile_prompt
        lowered = compiled.lower()
        facts = {key: key.lower() in lowered for key in spec.keys}
        recall_rows.append(
            {
                "role": spec.role,
                "objective": spec.objective,
                "required": list(spec.keys),
                "facts_in_packet": facts,
                "all_in_packet": all(facts.values()),
            }
        )
        compile_rows.append(
            {"role": spec.role, "objective": spec.objective, "prompt_tokens": compile_prompt}
        )
    return {
        "scenario": "pilot",
        "kind": "dry_run",
        "fabric": fabric,
        "preregistration": (
            "docs/engineering/PILOT-MECH-PREREGISTRATION.md"
            if fabric == "mech"
            else "docs/engineering/PILOT-PREREGISTRATION.md"
        ),
        "fixture": {
            "repo": PREGISTERED_REPO,
            "clone_path": str(fixture),
            "commit": commit,
            "commit_source": commit_source,
            "rendering": _RENDERING,
            "files": files,
            "bundle_tokens_tiktoken": count_tokens(rendered),
            "fabric": fabric,
        },
        "n_blocks": len(blocks),
        "arms": {
            "raw": {"prompt_tokens_total": raw_total, "objectives": raw_rows},
            "compile": {"prompt_tokens_total": compile_total, "objectives": compile_rows},
        },
        "recall": recall_rows,
    }


def _chat(
    client: httpx.Client,
    sidecar_url: str,
    *,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    headers: dict[str, str],
) -> _Completion:
    """One non-streaming chat completion through a sidecar (demo.py's, + request headers)."""
    started = time.perf_counter()
    response = client.post(
        f"{sidecar_url}/v1/chat/completions",
        json={
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "stream": False,
        },
        headers=headers,
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
        response.headers.get("x-fabric-compiled") == "1",
        "sha256:" + content_hash(messages),
    )


def _run_arm(
    client: httpx.Client,
    arm: str,
    url: str,
    metrics_url: str,
    specs: Sequence[PilotObjective],
    *,
    model: str,
    max_tokens: int,
    system_blob: str | None,
) -> tuple[list[dict[str, Any]], float, _Counters]:
    """Send every objective through one sidecar; score with the shared scorer."""
    before = _scrape_cache(client, metrics_url)
    stop_sampler = _start_kv_sampler(client, metrics_url)
    started = time.perf_counter()
    records: list[dict[str, Any]] = []
    for spec in specs:
        if arm == "raw":
            assert system_blob is not None
            messages = [
                {"role": "system", "content": system_blob},
                {"role": "user", "content": spec.objective},
            ]
            headers: dict[str, str] = {}
        else:
            messages = [{"role": "user", "content": spec.objective}]
            headers = {"X-Fabric-Agent": f"pilot-{spec.role}", "X-Fabric-Role": spec.role}
        try:
            completion = _chat(
                client, url, model=model, messages=messages, max_tokens=max_tokens, headers=headers
            )
            passed, missing = score_answer(completion.text, spec.keys)
            error = None
        except httpx.HTTPError as exc:
            completion = None
            passed, missing, error = False, list(spec.keys), str(exc)
        records.append(
            {
                "op": "chat",
                "arm": arm,
                "agent": spec.role,
                "objective": spec.objective,
                "required": list(spec.keys),
                "missing": missing,
                "passed": passed,
                "error": error,
                "compiled": completion.compiled if completion else False,
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
    counters = _Counters(
        _delta(before.hits, after.hits), _delta(before.queries, after.queries), stop_sampler()
    )
    return records, wall_ms, counters


def _sidecar_stats(client: httpx.Client, url: str) -> dict[str, Any]:
    try:
        response = client.get(f"{url}/fabric/stats", timeout=5.0)
        if response.status_code == 200:
            data = response.json()
            return {key: int(value) for key, value in data.items() if isinstance(value, int)}
    except (httpx.HTTPError, ValueError):
        pass
    return {}


def _criteria(
    arms: dict[str, dict[str, Any]],
    records: Sequence[dict[str, Any]],
    stats: dict[str, dict[str, Any]],
    total: int,
) -> dict[str, Any]:
    raw, compile_arm = arms["raw"], arms["compile"]
    compile_records = [record for record in records if record["arm"] == "compile"]
    error_rows = [record for record in records if record["error"]]
    raw_total = int(raw["prompt_tokens_total"])
    compile_total = int(compile_arm["prompt_tokens_total"])
    return {
        "a_token_reduction": {
            "pass": compile_total * 3 <= raw_total,
            "requirement": "compile total prompt tokens <= raw total / 3",
            "raw_prompt_tokens_total": raw_total,
            "compile_prompt_tokens_total": compile_total,
            "ratio_raw_over_compile": round(raw_total / compile_total, 2)
            if compile_total
            else None,
        },
        "b_accuracy_parity": {
            "pass": int(compile_arm["passed"]) >= int(raw["passed"]) - 1,
            "requirement": "compile passes >= raw passes - 1",
            "raw_passed": raw["passed"],
            "compile_passed": compile_arm["passed"],
            "total": total,
        },
        "c_no_compile_errors": {
            "pass": (
                not error_rows
                and stats.get("compile", {}).get("errors") == 0
                and stats.get("compile", {}).get("compiled") == len(compile_records)
                and all(record["compiled"] for record in compile_records)
            ),
            "requirement": (
                "zero failed requests; sidecar errors == 0; compiled == request count; every "
                "compile response carries X-Fabric-Compiled: 1"
            ),
            "error_rows": len(error_rows),
            "sidecar_stats": stats.get("compile", {}),
            "compiled_flags": sum(1 for record in compile_records if record["compiled"]),
            "compile_requests": len(compile_records),
        },
    }


def _print_dry_run(receipt: dict[str, Any]) -> None:
    fixture = receipt["fixture"]
    print(f"pilot: kind={receipt['kind']} fabric={receipt['fabric']}")
    print(f"preregistration: {receipt['preregistration']}")
    print(
        f"fixture: {fixture['repo']} @ {fixture['commit']} "
        f"({len(fixture['files'])} files, {fixture['bundle_tokens_tiktoken']} tiktoken)"
    )
    print(f"blocks: {receipt['n_blocks']}")
    raw = receipt["arms"]["raw"]
    compile_arm = receipt["arms"]["compile"]
    print(
        f"raw     prompt_tokens_total={raw['prompt_tokens_total']}"
        f"  (bundle {fixture['bundle_tokens_tiktoken']} + objective per request)"
    )
    print(f"compile prompt_tokens_total={compile_arm['prompt_tokens_total']}")
    for row in raw["objectives"]:
        print(f"  raw[{row['role']:<11}] {row['prompt_tokens']:>6} tok")
    for row in compile_arm["objectives"]:
        print(f"  cmp[{row['role']:<11}] {row['prompt_tokens']:>6} tok")
    print("recall (key present in compiled packet - OUTCOME, not a gate):")
    for row in receipt["recall"]:
        marks = " ".join(
            f"{key}={'Y' if hit else 'N'}" for key, hit in row["facts_in_packet"].items()
        )
        print(f"  [{row['role']:<11}] {marks}")


def _print_receipt(receipt: dict[str, Any]) -> None:
    print(f"pilot: kind={receipt['kind']} model={receipt['model']} upstream={receipt['upstream']}")
    print(f"fixture: {receipt['fixture']['repo']} @ {receipt['fixture']['commit']}")
    print(
        f"bundle: {len(receipt['fixture']['files'])} files,"
        f" {receipt['fixture']['bundle_tokens_tiktoken']} tiktoken"
    )
    print(
        f"cartridge: {receipt['cartridge']['cartridge_id']}"
        f" tokens={receipt['cartridge']['token_count']}"
    )
    print(f"{'arm':<9}{'reqs':>5}{'prompt_tok':>11}{'compl_tok':>10}{'mean_s':>8}  result")
    for name, arm in receipt["arms"].items():
        mean_s = float(arm.get("mean_latency_ms") or 0.0) / 1000
        print(
            f"{name:<9}{arm['requests']:>5}{arm['prompt_tokens_total']:>11}"
            f"{arm['completion_tokens_total']:>10}{mean_s:>8.2f}  acc"
            f" {arm['passed']}/{arm['total']}"
        )
    if receipt["cache"]["available"]:
        for name, cache_stats in receipt["cache"]["per_arm"].items():
            print(
                f"cache[{name}]: hits+={cache_stats['prefix_cache_hits_delta']}"
                f" queries+={cache_stats['prefix_cache_queries_delta']}"
                f" kv_peak={cache_stats['kv_cache_usage_peak']}"
            )
    for name, verdict in receipt["criteria"].items():
        print(
            f"criterion {name}: {'PASS' if verdict['pass'] else 'FAIL'} ({verdict['requirement']})"
        )


def run(
    client: httpx.Client,
    upstream: str,
    *,
    model: str,
    fixture: Path,
    max_tokens: int,
    smoke: bool,
    ts: str,
    fabric: str = "hand",
) -> dict[str, Any]:
    """Execute one pilot pass (smoke or full); returns the receipt dict."""
    started_at = datetime.now(tz=UTC).isoformat()
    specs = pilot_workload()[:1] if smoke else pilot_workload()
    kind = "smoke" if smoke else "full"

    rendered, files = render_bundle(fixture)
    bundle_tokens = count_tokens(rendered)
    if fabric == "mech":
        verify_raw_answerable(rendered, pilot_workload())
    else:
        verify_keys(rendered, pilot_workload(), _FACT_BLOCKS)
    commit, commit_source = commit_of(fixture)
    cartridge = compile_cartridge(fixture, commit)

    FIXTURES.mkdir(parents=True, exist_ok=True)
    manifest = {
        "repo": PREGISTERED_REPO,
        "clone_path": str(fixture),
        "commit": commit,
        "commit_source": commit_source,
        "rendering": _RENDERING,
        "files": files,
        "bundle_tokens_tiktoken": bundle_tokens,
        "preregistration": (
            "docs/engineering/PILOT-MECH-PREREGISTRATION.md"
            if fabric == "mech"
            else "docs/engineering/PILOT-PREREGISTRATION.md"
        ),
        "fabric": fabric,
        "ts": ts,
    }
    (FIXTURES / f"pilot_fixture_{ts}.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    cartridge_payload = {
        "cartridge_id": cartridge.cartridge_id,
        "tenant_id": cartridge.tenant_id,
        "project_id": cartridge.project_id,
        "commit_hash": cartridge.commit_hash,
        "token_count": cartridge.token_count,
        "kv_prefix_hash": cartridge.kv_prefix_hash,
        "hologram_markdown": cartridge.hologram_markdown,
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"pilot_cartridge_{ts}.json").write_text(
        json.dumps(cartridge_payload, indent=2), encoding="utf-8"
    )

    metrics_url = upstream.rstrip("/") + "/metrics"
    records: list[dict[str, Any]] = []
    arms: dict[str, dict[str, Any]] = {}
    per_arm_cache: dict[str, _Counters] = {}
    stats: dict[str, dict[str, Any]] = {}
    notes = [
        f"Fixture is REAL repository content: {PREGISTERED_REPO} @ {commit}; the raw arm's "
        f"system message is the {len(files)}-file bundle ({bundle_tokens} tiktoken) rendered "
        "with FILE headers (actual bytes, stable order); per-file sha256 in the fixture "
        "manifest so a fresh clone can audit the exact bytes.",
        "Raw ran first; all six raw requests share one identical system message (the strongest "
        "naive baseline, maximizing upstream prefix-cache reuse), only the user objective "
        "differs.",
        "The smoke pass (if any, same vLLM session) and the raw arm's shared system message "
        "warm the upstream prefix cache: cache deltas are warm-cache numbers, not cold-start.",
        (
            "Compile arm = the shipped sidecar's compile mode over an 11-block SQLite fabric "
            "(trust_domain 'default', lifecycle routable, fact kinds, source_event_seq 1..11)."
            if fabric == "hand"
            else f"Compile arm = the shipped sidecar's compile mode over a mechanical fabric "
            f"built by evals/mech_fabric.build_blocks from the same {len(BUNDLE_FILES)} bundle "
            "files (top-level symbols, assignments, import roots, and a project cartridge "
            "hologram); the ingester never reads objectives or answer keys, so whether a key "
            "reaches a compiled packet is an outcome (dry-run recall table), not a precondition."
        ),
        "The shipped server's compile path takes no cartridge argument, so the compiled "
        ".cfx Cartridge (artifact pilot_cartridge_*.json) is recorded but NOT injected: the "
        "compile arm measures store-only compilation (preregistered limitation).",
        "Sidecar compile budget is fixed at budget_base=4000 by the server; the rendered "
        "packet is injected as the system message.",
        "Accuracy is the same deterministic case-insensitive substring check for both arms "
        "(information presence, not answer quality); every objective and response is reported, "
        "pass or fail.",
        "Cache counters come from the upstream Prometheus /metrics (vLLM), parsed tolerantly "
        "(null when unavailable); prefix-cache counters are upstream-global; kv_cache_usage_peak "
        "is a best-effort ~50ms sample of the capacity fraction.",
        f"n={len(specs)} objectives x 1 repeat, temperature=0.0, non-streaming, "
        f"max_tokens={max_tokens}; single GPU, single 1.5B model.",
    ]

    with tempfile.TemporaryDirectory(prefix="cfx_pilot_", ignore_cleanup_errors=True) as tmp:
        if fabric == "hand":
            store_path = build_pilot_store(Path(tmp) / "pilot.db")
        else:
            store_path = build_mech_store(Path(tmp) / "pilot.db", fixture)
        compile_store = open_store(store_path)
        blocks = tuple(compile_store.load_blocks())
        servers = {
            "raw": make_server(upstream=upstream, mode="passthrough", port=0),
            "compile": make_server(
                upstream=upstream, mode="compile", blocks=blocks, port=0, store=compile_store
            ),
        }
        urls = {arm: _serve(server) for arm, server in servers.items()}
        try:
            for arm, url in urls.items():
                arm_records, wall_ms, per_arm_cache[arm] = _run_arm(
                    client,
                    arm,
                    url,
                    metrics_url,
                    specs,
                    model=model,
                    max_tokens=max_tokens,
                    system_blob=rendered if arm == "raw" else None,
                )
                records.extend(arm_records)
                arms[arm] = {
                    **_totals(arm_records, wall_ms),
                    "passed": sum(1 for record in arm_records if record["passed"]),
                    "total": len(arm_records),
                }
                stats[arm] = _sidecar_stats(client, url)
        finally:
            _stop(*servers.values())

    receipt: dict[str, Any] = {
        "scenario": "pilot",
        "kind": kind,
        "fabric": fabric,
        "model": model,
        "upstream": upstream,
        "started_at": started_at,
        "ts": ts,
        "fixture": manifest,
        "cartridge": cartridge_payload,
        "objectives": [
            {"role": spec.role, "objective": spec.objective, "required": list(spec.keys)}
            for spec in specs
        ],
        "records": records,
        "arms": arms,
        "cache": {
            "available": any(
                value is not None for counters in per_arm_cache.values() for value in counters
            ),
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
        "sidecar_stats": stats,
        "notes": notes,
    }
    receipt["criteria"] = {} if smoke else _criteria(arms, records, stats, len(specs))
    return receipt


def _upstream_url(raw: str) -> str:
    """CF_BASE_URL-style values end in /v1; the sidecar wants the bare upstream root."""
    return raw.rstrip("/")[:-3].rstrip("/") if raw.rstrip("/").endswith("/v1") else raw.rstrip("/")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.pilot",
        description="Pre-registered pilot: raw-httpx-bundle vs compiled-packet A/B.",
    )
    parser.add_argument(
        "--fixture",
        default=os.environ.get("CF_PILOT_FIXTURE", ""),
        help="path to the httpx clone (env: CF_PILOT_FIXTURE)",
    )
    parser.add_argument(
        "--upstream",
        default=os.environ.get("CF_UPSTREAM")
        or os.environ.get("CF_BASE_URL")
        or "http://127.0.0.1:8000",
        help="OpenAI-compatible upstream root (env: CF_UPSTREAM / CF_BASE_URL)",
    )
    parser.add_argument("--model", default=os.environ.get("CF_MODEL", "Qwen/Qwen2.5-1.5B-Instruct"))
    parser.add_argument("--smoke", action="store_true", help="1 objective, tiny max_tokens")
    parser.add_argument("--max-tokens", type=int, default=None, help="default: 8 smoke, 48 full")
    parser.add_argument(
        "--fabric",
        choices=("hand", "mech"),
        default="hand",
        help="hand = the 11 frozen hand-derived blocks (original pilot); mech = mechanical "
        "fabric via evals/mech_fabric.build_blocks",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build both arms offline (no network/model) and print token counts + recall",
    )
    parser.add_argument("--out", default="", help="receipt path override")
    args = parser.parse_args(argv)

    if not args.fixture:
        print("pilot: --fixture (or CF_PILOT_FIXTURE) is required", file=sys.stderr)
        return 2
    fixture = Path(args.fixture)
    if not (fixture / "httpx" / "_client.py").is_file():
        print(f"pilot: {fixture} does not look like an httpx clone", file=sys.stderr)
        return 2
    if args.dry_run:
        ts = time.strftime("%Y%m%d_%H%M%S")
        receipt = dry_run(fixture, fabric=args.fabric)
        REPORTS.mkdir(parents=True, exist_ok=True)
        out = Path(args.out) if args.out else REPORTS / f"pilot_dryrun_{ts}.json"
        out.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        _print_dry_run(receipt)
        print(f"receipt: {out}")
        return 0
    max_tokens = args.max_tokens if args.max_tokens is not None else (8 if args.smoke else 48)
    if max_tokens <= 0:
        print("pilot: --max-tokens must be positive", file=sys.stderr)
        return 2
    upstream = _upstream_url(args.upstream)
    ts = time.strftime("%Y%m%d_%H%M%S")
    with httpx.Client(timeout=900.0, trust_env=False) as client:
        try:
            receipt = run(
                client,
                upstream,
                model=args.model,
                fixture=fixture,
                max_tokens=max_tokens,
                smoke=args.smoke,
                ts=ts,
                fabric=args.fabric,
            )
        except httpx.HTTPError as exc:
            print(f"pilot: request failed: {exc}", file=sys.stderr)
            return 2
    REPORTS.mkdir(parents=True, exist_ok=True)
    name = f"pilot{'_smoke' if args.smoke else ''}_{ts}.json"
    out = Path(args.out) if args.out else REPORTS / name
    out.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    _print_receipt(receipt)
    print(f"receipt: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
