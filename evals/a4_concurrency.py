"""Pre-registered A4 concurrency benchmark — raw-repo vs compiled-packet under load.

At N concurrent sub-agents (N in 1, 4, 8, 16, 32) on one RTX 5070 8 GB with vLLM prefix
caching and Qwen2.5-1.5B-Instruct, measure throughput (req/s), p50/p99 latency, queueing
(vLLM running/waiting gauges), peak KV usage, and request errors for two arms:

  * R raw     : every agent gets the full shared httpx bundle as an identical system
                message (the strongest naive baseline — identical prefix, so vLLM caches
                it once and the N agents share it via copy-on-write).
  * C compile : per-agent compiled packet with the shared Tier-0 cartridge FIRST (the
                cartridge hologram leads, so the N agents share that prefix; the rest of
                each packet — instruction + recalled blocks — diverges per objective).

Design, objectives, arms, metrics, comparison rule, and error counting are frozen in
``docs/engineering/A4-CONCURRENCY-PREREGISTRATION.md`` before any run.

Run (one arm per vLLM session; vLLM restarted between arms):

    ~/.venvs/vllm/bin/python -m evals.a4_concurrency --arm raw    --fixture <httpx clone>
    ~/.venvs/vllm/bin/python -m evals.a4_concurrency --arm compile --fixture <httpx clone>

Offline dry-run (no model, no network) prints the token table:

    uv run python -m evals.a4_concurrency --dry-run --fixture <httpx clone>

Artifacts: ``evals/reports/a4_<arm>_<ts>.json`` (live) and ``a4_dryrun_<ts>.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from evals.mech_fabric import build_blocks, build_project_cartridge
from evals.pilot import BUNDLE_FILES, render_bundle
from kernel.cartridge.serializer import Cartridge, count_tokens
from kernel.router.compiler import DEFAULT_AS_OF, compile_packet
from protocol.models import ContextBlock, Recipient, Scope
from sidecar.render import render_packet

REPORTS = Path(__file__).parent / "reports"
PREREGISTRATION = "docs/engineering/A4-CONCURRENCY-PREREGISTRATION.md"

N_LEVELS: tuple[int, ...] = (1, 4, 8, 16, 32)
DEFAULT_REPEATS = 3
DEFAULT_MAX_TOKENS = 64
MAX_MODEL_LEN = 32768
DEFAULT_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
DEFAULT_FIXTURE = r"C:\Users\<user>\AppData\Local\Temp\opencode\httpx_pilot"

_ARM_RAW = "raw"
_ARM_COMPILE = "compile"
_ROLE = "subagent"
_GAUGES = ("kv_cache_usage_perc", "num_requests_running", "num_requests_waiting")

# Substrings that mark an upstream context-length / max-model-len rejection (HTTP 400).
_CONTEXT_LEN_MARKERS = (
    "maximum context length",
    "maximum model length",
    "maximum sequence length",
    "prompt length",
    "context length",
    "input is too long",
    "model's maximum",
    "exceeds the model",
    "exceeds maximum",
)

# Frozen set of per-agent objectives (one per sub-agent, in order). Each references a
# real top-level symbol of the httpx bundle so the mechanical fabric's recall has
# something concrete to match and the raw arm's full context contains the answer.
_OBJECTIVE_SYMBOLS: tuple[str, ...] = (
    "Client",
    "AsyncClient",
    "BaseClient",
    "Timeout",
    "Limits",
    "Proxy",
    "request",
    "stream",
    "get",
    "post",
    "head",
    "put",
    "patch",
    "delete",
    "Auth",
    "BasicAuth",
    "DigestAuth",
    "FunctionAuth",
    "NetRCAuth",
    "GZipDecoder",
    "DeflateDecoder",
    "BrotliDecoder",
    "ZStandardDecoder",
    "HTTPError",
    "RequestError",
    "TimeoutException",
    "TooManyRedirects",
    "HTTPStatusError",
    "InvalidURL",
    "encode_json",
    "encode_request",
    "main",
)

_SCOPE = Scope(tenant_id="default", project_id="default", workflow_id="chat", task_id="chat")


def objective(i: int) -> str:
    return (
        f"In the httpx repository, which file defines `{_OBJECTIVE_SYMBOLS[i]}`? "
        "Answer with the file path only."
    )


def cache_tag(arm: str, n: int, repeat: int) -> str:
    """Unique leading prefix per (arm, N, repeat) so vLLM's cache cannot carry over."""
    return f"A4::{arm}::{n}::{repeat}::"


def build_fabric(fixture: Path) -> tuple[Cartridge, tuple[ContextBlock, ...]]:
    """Tier-0 cartridge + mechanical blocks, with the cartridge artifact block removed.

    The cartridge is pinned as the shared Tier-0 prefix via ``compile_packet(cartridge=…)``,
    so it must not also appear as a normal fabric block (that would duplicate it).
    """
    cartridge = build_project_cartridge(fixture, BUNDLE_FILES)
    blocks = tuple(
        block for block in build_blocks(fixture, BUNDLE_FILES) if block.block_id != "blk_cartridge"
    )
    return cartridge, blocks


def compile_systems(cartridge: Cartridge, blocks: Sequence[ContextBlock], n: int) -> list[str]:
    """Rendered compile system messages (no cache tag) for objectives 0..n-1."""
    out: list[str] = []
    for i in range(n):
        recipient = Recipient(agent_id=f"a4-{i:02d}", role=_ROLE, stage="chat")
        result = compile_packet(
            scope=_SCOPE,
            recipient=recipient,
            objective=objective(i),
            stage="chat",
            blocks=blocks,
            cartridge=cartridge,
            watermark_event_seq=0,
            budget_base=4000,
            as_of=DEFAULT_AS_OF,
        )
        out.append(render_packet(result.packet))
    return out


def dry_run(fixture: Path) -> dict[str, Any]:
    """Offline build of both arms (no network, no model): token table + fit check.

    The raw arm is the rendered bundle + objective; the compile arm is the sidecar-equivalent
    compile path (budget_base=4000) with the cartridge pinned as shared Tier-0, rendered
    client-side. Raises before any request if an objective symbol is absent from the bundle
    (a fixture/content error, not a result).
    """
    bundle, files = render_bundle(fixture)
    bundle_tokens = count_tokens(bundle)
    lowered = bundle.lower()
    missing = [symbol for symbol in _OBJECTIVE_SYMBOLS if symbol.lower() not in lowered]
    if missing:
        raise ValueError(
            f"objective symbols missing from raw bundle: {missing} "
            "(fixture/content error, run aborted before any request)"
        )
    cartridge, blocks = build_fabric(fixture)
    systems = compile_systems(cartridge, blocks, len(_OBJECTIVE_SYMBOLS))

    raw_rows: list[dict[str, Any]] = []
    compile_rows: list[dict[str, Any]] = []
    raw_total = compile_total = 0
    for i, symbol in enumerate(_OBJECTIVE_SYMBOLS):
        obj = objective(i)
        raw_prompt = bundle_tokens + count_tokens(obj)
        compile_prompt = count_tokens(systems[i]) + count_tokens(obj)
        raw_total += raw_prompt
        compile_total += compile_prompt
        raw_rows.append({"i": i, "symbol": symbol, "objective": obj, "prompt_tokens": raw_prompt})
        compile_rows.append(
            {"i": i, "symbol": symbol, "objective": obj, "prompt_tokens": compile_prompt}
        )

    # Raw-arm fit: the largest raw prompt (bundle + longest objective) + max_tokens must
    # fit MAX_MODEL_LEN, leaving headroom for the chat template (measured at run time).
    longest_objective = max(count_tokens(objective(i)) for i in range(len(_OBJECTIVE_SYMBOLS)))
    raw_fit = bundle_tokens + longest_objective + DEFAULT_MAX_TOKENS
    return {
        "scenario": "a4_concurrency",
        "kind": "dry_run",
        "preregistration": PREREGISTRATION,
        "fixture": {
            "path": str(fixture),
            "files": len(files),
            "bundle_tokens_tiktoken": bundle_tokens,
        },
        "cartridge": {
            "cartridge_id": cartridge.cartridge_id,
            "token_count": cartridge.token_count,
            "kv_prefix_hash": cartridge.kv_prefix_hash,
        },
        "config": {
            "n_levels": list(N_LEVELS),
            "repeats": DEFAULT_REPEATS,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "temperature": 0.0,
            "max_model_len": MAX_MODEL_LEN,
            "model": DEFAULT_MODEL,
        },
        "arms": {
            "raw": {"prompt_tokens_total": raw_total, "per_objective": raw_rows},
            "compile": {"prompt_tokens_total": compile_total, "per_objective": compile_rows},
        },
        "raw_fit": {
            "bundle_tokens": bundle_tokens,
            "longest_objective_tokens": longest_objective,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "raw_bundle_plus_objective_plus_max_tokens": raw_fit,
            "headroom_tokens": MAX_MODEL_LEN - raw_fit,
            "fits": raw_fit < MAX_MODEL_LEN,
            "note": (
                "tiktoken (o200k) counts; the Qwen chat template adds a few tokens and is "
                "measured at run time. The pilot served this exact bundle at max-model-len "
                "32768 successfully."
            ),
        },
    }


def classify_error(status: int, body: str) -> str:
    """Classify a request outcome: ok / client_error / context_length / http_error."""
    if status == 200:
        return "ok"
    if status == 0:
        return "client_error"
    lowered = body.lower()
    if any(marker in lowered for marker in _CONTEXT_LEN_MARKERS):
        return "context_length"
    return "http_error"


def percentile(ordered: list[float], pct: float) -> float:
    """Nearest-rank percentile of an already-sorted list (empty -> 0.0)."""
    if not ordered:
        return 0.0
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def parse_gauges(text: str) -> dict[str, float]:
    out: dict[str, float] = dict.fromkeys(_GAUGES, 0.0)
    for line in text.splitlines():
        if not line.startswith("vllm:"):
            continue
        head, _, value = line.rpartition(" ")
        key = head.split("{")[0].split(":", 1)[1]
        if key in out:
            try:
                out[key] = float(value)
            except ValueError:
                pass
    return out


async def sample_gauges(
    stop: asyncio.Event, metrics_url: str, samples: list[dict[str, float]]
) -> None:
    async with httpx.AsyncClient(timeout=5.0) as client:
        while not stop.is_set():
            try:
                response = await client.get(metrics_url)
                if response.status_code == 200:
                    samples.append(parse_gauges(response.text))
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.025)


async def one_request(
    client: httpx.AsyncClient,
    base_url: str,
    key: str,
    model: str,
    msgs: list[dict[str, str]],
    max_tokens: int,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": msgs,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": False,
    }
    start = time.perf_counter()
    try:
        response = await client.post(
            f"{base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {key}"},
        )
    except httpx.HTTPError as exc:
        return {
            "status": 0,
            "error_kind": type(exc).__name__,
            "latency_ms": (time.perf_counter() - start) * 1000.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }
    latency_ms = (time.perf_counter() - start) * 1000.0
    usage: dict[str, Any] = {}
    if response.status_code == 200:
        try:
            usage = response.json().get("usage") or {}
        except ValueError:
            usage = {}
    return {
        "status": response.status_code,
        "error_kind": classify_error(response.status_code, response.text),
        "latency_ms": latency_ms,
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
    }


async def burst(
    base_url: str,
    key: str,
    model: str,
    metrics_url: str,
    msgs_list: list[list[dict[str, str]]],
    max_tokens: int,
) -> dict[str, Any]:
    samples: list[dict[str, float]] = []
    stop = asyncio.Event()
    sampler = asyncio.create_task(sample_gauges(stop, metrics_url, samples))
    async with httpx.AsyncClient(timeout=600.0) as client:
        start = time.perf_counter()
        results = await asyncio.gather(
            *(one_request(client, base_url, key, model, m, max_tokens) for m in msgs_list)
        )
        wall_s = time.perf_counter() - start
    stop.set()
    await sampler
    latencies = sorted(float(r["latency_ms"]) for r in results)
    errors = [r for r in results if r["status"] != 200]
    error_kinds: dict[str, int] = {}
    for r in errors:
        error_kinds[str(r["error_kind"])] = error_kinds.get(str(r["error_kind"]), 0) + 1
    peak = {g: max((s.get(g, 0.0) for s in samples), default=0.0) for g in _GAUGES}
    return {
        "requests": len(results),
        "wall_s": round(wall_s, 3),
        "throughput_req_per_s": round(len(results) / wall_s, 2) if wall_s > 0 else 0.0,
        "p50_ms": round(percentile(latencies, 50), 2),
        "p99_ms": round(percentile(latencies, 99), 2),
        "peak_kv_cache_usage_perc": peak["kv_cache_usage_perc"],
        "peak_requests_running": peak["num_requests_running"],
        "peak_requests_waiting": peak["num_requests_waiting"],
        "errors": len(errors),
        "error_kinds": error_kinds,
        "prompt_tokens_total": sum(int(r["prompt_tokens"]) for r in results),
        "completion_tokens_total": sum(int(r["completion_tokens"]) for r in results),
    }


async def run_arm(
    base_url: str,
    key: str,
    model: str,
    arm: str,
    fixture: Path,
    n_levels: Sequence[int],
    repeats: int,
    max_tokens: int,
) -> dict[str, Any]:
    metrics_url = base_url.rsplit("/v1", 1)[0] + "/metrics"
    bundle, files = render_bundle(fixture)
    systems: list[str] = []
    if arm == _ARM_COMPILE:
        cartridge, blocks = build_fabric(fixture)
        systems = compile_systems(cartridge, blocks, max(n_levels))

    cells: list[dict[str, Any]] = []
    for n in n_levels:
        for repeat in range(1, repeats + 1):
            tag = cache_tag(arm, n, repeat)
            msgs_list: list[list[dict[str, str]]] = []
            for i in range(n):
                system = f"{tag}\n{bundle}" if arm == _ARM_RAW else f"{tag}\n{systems[i]}"
                msgs_list.append(
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": objective(i)},
                    ]
                )
            cell = await burst(base_url, key, model, metrics_url, msgs_list, max_tokens)
            cell["arm"] = arm
            cell["n"] = n
            cell["repeat"] = repeat
            cell["tag"] = tag
            cells.append(cell)

    return {
        "scenario": "a4_concurrency",
        "arm": arm,
        "preregistration": PREREGISTRATION,
        "model": model,
        "upstream": base_url,
        "started_at": datetime.now(tz=UTC).isoformat(),
        "fixture": {
            "path": str(fixture),
            "files": len(files),
            "bundle_tokens_tiktoken": count_tokens(bundle),
        },
        "config": {
            "n_levels": list(n_levels),
            "repeats": repeats,
            "max_tokens": max_tokens,
            "temperature": 0.0,
        },
        "cells": cells,
        "notes": [
            "Each repeat uses a fresh cache tag, so vLLM's persistent prefix cache cannot "
            "carry between repeats; within a repeat all N agents share one tag (and thus "
            "the shared prefix) and the first-scheduled request cold-prefills it while the "
            "rest ride the copy-on-write cache.",
            "vLLM is restarted between arms (one arm per invocation), so no prefix-cache "
            "state carries between raw and compile.",
            "peak gauges are sampled at ~25 ms (may miss faster transients); running/waiting "
            "are vLLM scheduler gauges, kv_cache_usage_perc is the fixed KV-cache metric.",
        ],
    }


def _up(base_url: str) -> bool:
    try:
        with httpx.Client(timeout=3.0) as client:
            return client.get(f"{base_url.rstrip('/')}/models").status_code < 500
    except httpx.HTTPError:
        return False


def _print_dry_run(receipt: dict[str, Any]) -> None:
    fixture = receipt["fixture"]
    cartridge = receipt["cartridge"]
    fit = receipt["raw_fit"]
    print("a4_concurrency dry-run (offline, no model/network)")
    print(f"preregistration: {receipt['preregistration']}")
    print(
        f"fixture: {fixture['path']} ({fixture['files']} files, "
        f"{fixture['bundle_tokens_tiktoken']} tiktoken)"
    )
    print(
        f"cartridge: {cartridge['cartridge_id']} "
        f"tokens={cartridge['token_count']} kv_prefix={cartridge['kv_prefix_hash']}"
    )
    print(
        f"raw fit: bundle {fit['bundle_tokens']} + longest objective "
        f"{fit['longest_objective_tokens']} + max_tokens {fit['max_tokens']} = "
        f"{fit['raw_bundle_plus_objective_plus_max_tokens']} "
        f"(headroom {fit['headroom_tokens']}, fits={fit['fits']})"
    )
    print()
    print(f"{'i':>2} {'symbol':<16} {'raw':>6} {'compile':>8}")
    for raw_row, cmp_row in zip(
        receipt["arms"]["raw"]["per_objective"],
        receipt["arms"]["compile"]["per_objective"],
        strict=True,
    ):
        print(
            f"{raw_row['i']:>2} {raw_row['symbol']:<16} "
            f"{raw_row['prompt_tokens']:>6} {cmp_row['prompt_tokens']:>8}"
        )
    print(
        f"{'':>2} {'total':<16} "
        f"{receipt['arms']['raw']['prompt_tokens_total']:>6} "
        f"{receipt['arms']['compile']['prompt_tokens_total']:>8}"
    )


def _print_cells(report: dict[str, Any]) -> None:
    print(f"a4_concurrency arm={report['arm']} model={report['model']}")
    print(
        f"{'n':>3} {'rep':>3} | {'req/s':>7} {'p50':>8} {'p99':>8} "
        f"{'peakKV%':>8} {'run':>4} {'wait':>5} {'errs':>5} {'ctxlen':>7}"
    )
    for cell in report["cells"]:
        kinds = cell["error_kinds"]
        print(
            f"{cell['n']:>3} {cell['repeat']:>3} | {cell['throughput_req_per_s']:>7.2f} "
            f"{cell['p50_ms']:>8.1f} {cell['p99_ms']:>8.1f} "
            f"{cell['peak_kv_cache_usage_perc']:>8.2f} "
            f"{cell['peak_requests_running']:>4.0f} {cell['peak_requests_waiting']:>5.0f} "
            f"{cell['errors']:>5} {kinds.get('context_length', 0):>7}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.a4_concurrency")
    parser.add_argument(
        "--fixture",
        default=os.environ.get("CF_A4_FIXTURE", DEFAULT_FIXTURE),
        help="path to the httpx clone (env: CF_A4_FIXTURE)",
    )
    parser.add_argument(
        "--arm",
        choices=(_ARM_RAW, _ARM_COMPILE),
        help="arm to run against a live vLLM (omit with --dry-run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build both arms offline (no network/model) and print the token table",
    )
    parser.add_argument(
        "--upstream",
        default=os.environ.get("CF_BASE_URL", "http://localhost:8000/v1"),
        help="OpenAI-compatible upstream (env: CF_BASE_URL)",
    )
    parser.add_argument("--model", default=os.environ.get("CF_MODEL", DEFAULT_MODEL))
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument(
        "--n", default=",".join(str(n) for n in N_LEVELS), help="comma-separated N levels"
    )
    parser.add_argument("--out", default="", help="receipt path override")
    args = parser.parse_args(argv)

    fixture = Path(args.fixture)
    if not (fixture / "httpx" / "_client.py").is_file():
        print(f"a4_concurrency: {fixture} does not look like an httpx clone", file=sys.stderr)
        return 2

    n_levels = tuple(int(part) for part in str(args.n).split(","))
    if args.max_tokens <= 0 or args.repeats <= 0 or not n_levels:
        print("a4_concurrency: --max-tokens and --repeats must be positive", file=sys.stderr)
        return 2

    if args.dry_run:
        receipt = dry_run(fixture)
        REPORTS.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        out = Path(args.out) if args.out else REPORTS / f"a4_dryrun_{ts}.json"
        out.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        _print_dry_run(receipt)
        print(f"\nreceipt: {out}")
        return 0

    if args.arm is None:
        print("a4_concurrency: --arm is required for a live run (see --dry-run)", file=sys.stderr)
        return 2

    base_url = args.upstream.rstrip("/")
    if not _up(base_url):
        print("No endpoint. See docs/engineering/RUNTIME.md (or run scripts/wsl_run_eval.sh).")
        return 0

    key = os.environ.get("CF_API_KEY", "EMPTY")
    report = asyncio.run(
        run_arm(
            base_url,
            key,
            args.model,
            args.arm,
            fixture,
            n_levels,
            args.repeats,
            args.max_tokens,
        )
    )
    REPORTS.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) if args.out else REPORTS / f"a4_{args.arm}_{ts}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    _print_cells(report)
    print(f"\nreport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
