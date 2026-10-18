"""Stage B5 — concurrency / VRAM behaviour under many in-flight agents.

The beachhead's actual pain is the KV-cache/VRAM wall. This fires K agents CONCURRENTLY
and measures vLLM's KV-cache utilisation, running/waiting request counts, throughput, and
wall time:

  * B0_divergent : each child carries a distinct full context (no sharing) -> KV pressure.
  * CPP_shared   : children share one Tier 0+1 prefix, distinct small tails -> sharing.

Prediction under the thesis: CPP_shared keeps peak KV usage lower and finishes more work
per second. If it does not, that is a finding (GAP-23/GAP-24 in docs/SHORTFALL-AUDIT.md).

Run: uv run python -m evals.stage_b5
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from evals.stage_b2 import CARTRIDGE, INSTRUCTION, facts, pointers

REPORTS = Path(__file__).parent / "reports"
K_LEVELS = (4, 8, 16)
MAX_K = max(K_LEVELS)
GAUGES = ("kv_cache_usage_perc", "num_requests_running", "num_requests_waiting")


def obs_block(i: int, lines: int = 10) -> str:
    return f"=== tool result {i} ===\n" + "\n".join(
        f"  line {j}: raw source or log content {'x' * 12}" for j in range(lines)
    )


def raw_ctx(child: int, tag: str) -> str:
    body = "\n".join(obs_block(i) for i in range(15))
    return f"CHILD {child} [{tag}]\n{body}"  # unique per child -> divergent KV


def shared_prefix(tag: str) -> str:
    # Leading tag makes each case's prefix distinct so vLLM cache cannot carry between cases.
    return f"CASE {tag}\n{CARTRIDGE}\n{INSTRUCTION}\nLEDGER:\n{facts(15)}\nSWAP:\n{pointers(15)}"


def parse_gauges(text: str) -> dict[str, float]:
    out = dict.fromkeys(GAUGES, 0.0)
    for line in text.splitlines():
        if not line.startswith("vllm:"):
            continue
        head, _, val = line.rpartition(" ")
        key = head.split("{")[0].split(":", 1)[1]
        if key in out:
            try:
                out[key] = float(val)
            except ValueError:
                pass
    return out


async def sample_metrics(stop: asyncio.Event, url: str, samples: list[dict[str, float]]) -> None:
    async with httpx.AsyncClient(timeout=5.0) as c:
        while not stop.is_set():
            try:
                r = await c.get(url)
                if r.status_code == 200:
                    samples.append(parse_gauges(r.text))
            except Exception:
                pass
            await asyncio.sleep(0.1)


async def one_request(
    client: httpx.AsyncClient, base_url: str, key: str, msgs: list[dict[str, str]]
) -> dict[str, Any]:
    payload = {
        "model": os.environ.get("CF_MODEL", "Qwen/Qwen2.5-1.5B-Instruct"),
        "messages": msgs,
        "max_tokens": 16,
        "temperature": 0.0,
    }
    r = await client.post(
        f"{base_url}/chat/completions", json=payload, headers={"Authorization": f"Bearer {key}"}
    )
    r.raise_for_status()
    usage: dict[str, Any] = r.json().get("usage", {})
    return usage


async def burst(
    base_url: str,
    key: str,
    metrics_url: str,
    msgs_list: list[list[dict[str, str]]],
) -> dict[str, Any]:
    samples: list[dict[str, float]] = []
    stop = asyncio.Event()
    sampler = asyncio.create_task(sample_metrics(stop, metrics_url, samples))
    async with httpx.AsyncClient(timeout=300.0) as client:
        start = time.perf_counter()
        results = await asyncio.gather(*(one_request(client, base_url, key, m) for m in msgs_list))
        wall_ms = (time.perf_counter() - start) * 1000
    stop.set()
    await sampler
    peak = {g: max((s.get(g, 0.0) for s in samples), default=0.0) for g in GAUGES}
    return {
        "concurrency": len(msgs_list),
        "wall_ms": wall_ms,
        "throughput_req_per_s": len(msgs_list) / (wall_ms / 1000),
        "peak_kv_cache_usage_perc": peak["kv_cache_usage_perc"],
        "peak_requests_running": peak["num_requests_running"],
        "peak_requests_waiting": peak["num_requests_waiting"],
        "prompt_tokens_total": sum(int(r.get("prompt_tokens", 0)) for r in results),
    }


def build_arms(tag: str) -> dict[str, list[list[dict[str, str]]]]:
    arms: dict[str, list[list[dict[str, str]]]] = {"B0_divergent": [], "CPP_shared": []}
    prefix = shared_prefix(tag)
    for i in range(MAX_K):
        arms["B0_divergent"].append(
            [
                {"role": "system", "content": INSTRUCTION},
                {"role": "user", "content": raw_ctx(i, tag)},
            ]
        )
        arms["CPP_shared"].append(
            [
                {"role": "system", "content": prefix},
                {"role": "user", "content": f"ROLE/TASK: sub-task {i}\nNext action?"},
            ]
        )
    return arms


async def run_all() -> dict[str, Any]:
    base_url = os.environ.get("CF_BASE_URL", "http://localhost:8000/v1")
    key = os.environ.get("CF_API_KEY", "EMPTY")
    metrics_url = base_url.rsplit("/v1", 1)[0] + "/metrics"
    report: dict[str, Any] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "cases": []}
    for arm in ("B0_divergent", "CPP_shared"):
        for k in K_LEVELS:
            arms = build_arms(f"{arm}-k{k}")
            r = await burst(base_url, key, metrics_url, arms[arm][:k])
            r["arm"] = arm
            report["cases"].append(r)
    return report


def main() -> int:
    base_url = os.environ.get("CF_BASE_URL", "http://localhost:8000/v1")
    try:
        up = httpx.get(f"{base_url.rstrip('/')}/models", timeout=3.0).status_code < 500
    except Exception:
        up = False
    if not up:
        print("No endpoint. See docs/engineering/RUNTIME.md (or run scripts/wsl_run_eval.sh).")
        return 0

    report = asyncio.run(run_all())
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"stage_b5_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    hdr = (
        f"{'arm':>13} {'k':>3} | {'wall ms':>8} {'req/s':>7} "
        f"{'peakKV%':>8} {'run':>4} {'wait':>5} {'promptTok':>10}"
    )
    print(hdr)
    for c in report["cases"]:
        print(
            f"{c['arm']:>13} {c['concurrency']:>3} | {c['wall_ms']:>8.0f} "
            f"{c['throughput_req_per_s']:>7.2f} {c['peak_kv_cache_usage_perc']:>8.3f} "
            f"{c['peak_requests_running']:>4.0f} {c['peak_requests_waiting']:>5.0f} "
            f"{c['prompt_tokens_total']:>10}"
        )
    print(f"\nReport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
