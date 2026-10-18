"""Offline sidecar concurrency benchmark — in-process servers, fake upstream (no GPU).

The fake upstream returns canned OpenAI-compatible JSON instantly, so client-visible
latency is the sidecar's own cost: HTTP handling, request proxying, and (in compile mode)
compile+render plus the manifest-log write. N thread clients each send M sequential
requests in both modes at every concurrency level; the report records wall time,
throughput, and p50/p95/p99 latency per mode per N, plus the compile-vs-passthrough
latency delta at N=1.

Everything (fake upstream, sidecar, clients) runs in one process on one GIL, so these
numbers are a concurrency-structure probe, not a deployment capacity number; real
workloads are dominated by model inference. Compile mode uses an in-memory SQLite store
with manifest logging enabled, so the sidecar's write-lock path is exercised without
disk-I/O noise.

Run: ``uv run python -m evals.bench_sidecar`` (writes
``evals/reports/bench_sidecar_<ts>.json``; about a minute on the dev box).
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import statistics
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

from kernel.cartridge.serializer import count_tokens
from kernel.store import SqliteStore
from protocol.canonical import sha256_hex
from protocol.models import (
    BlockKind,
    ContextBlock,
    Creator,
    CreatorType,
    Fidelity,
    MemoryTier,
)
from sidecar.server import make_server, open_store

REPORTS = Path(__file__).parent / "reports"

DEFAULT_CONCURRENCY = (1, 4, 16, 64)
DEFAULT_REQUESTS = 20
DEFAULT_BLOCKS = 50


class _FakeUpstream(BaseHTTPRequestHandler):
    """Canned OpenAI-compatible upstream: instant JSON, no sleeps, no model."""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = {
            "id": "cmpl-bench",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8},
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


class _FakeUpstreamServer(ThreadingHTTPServer):
    """The instrument must not throttle the subject: accept bursts far past N=64."""

    request_queue_size = 512


def _serve(server: ThreadingHTTPServer) -> str:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


def _fabric(n: int) -> tuple[ContextBlock, ...]:
    blocks: list[ContextBlock] = []
    for i in range(n):
        content = (
            f"Bench decision {i}: keep routing entry {i} on shard {i % 7} with the "
            f"{30 + i % 11}-day retention window."
        )
        blocks.append(
            ContextBlock(
                block_id=f"bench-{i:03d}",
                block_kind=BlockKind.decision,
                memory_tier=MemoryTier.ledger,
                content=content,
                content_hash="sha256:" + sha256_hex(content.encode("utf-8")),
                token_estimate=count_tokens(content),
                source_event_seq=i + 1,
                creator=Creator(type=CreatorType.agent, id="bench"),
                fidelity=Fidelity.extract,
                trust_domain="default",
            )
        )
    return tuple(blocks)


def _percentile(ordered: list[float], pct: float) -> float:
    """Nearest-rank percentile of an already-sorted non-empty list."""
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def _worker(
    url: str,
    mode: str,
    cell: int,
    worker_id: int,
    *,
    requests: int,
    barrier: threading.Barrier,
) -> list[tuple[float, float, int, str]]:
    """Send ``requests`` sequential chat completions; one sample per request.

    Sample: ``(t0, t1, status, error_kind)``. The objective embeds ``cell`` so no two
    requests in the whole run compile the same objective (a repeat would hit the sidecar's
    manifest dedupe and skip the log insert, making the cell incomparable).
    """
    samples: list[tuple[float, float, int, str]] = []
    with httpx.Client(timeout=30.0) as client:
        barrier.wait(timeout=30.0)
        for j in range(requests):
            payload = {
                "model": "bench",
                "messages": [
                    {"role": "user", "content": f"{mode} bench cell {cell} request {worker_id}-{j}"}
                ],
            }
            start = time.perf_counter()
            status, error_kind = -1, ""
            try:
                status = client.post(f"{url}/v1/chat/completions", json=payload).status_code
            except httpx.HTTPError as exc:
                error_kind = type(exc).__name__
            samples.append((start, time.perf_counter(), status, error_kind))
    return samples


def _phase(url: str, mode: str, cell: int, concurrency: int, requests: int) -> dict[str, Any]:
    """Run one (mode, N) cell: N clients released together by a barrier, M requests each."""
    barrier = threading.Barrier(concurrency)
    worker = partial(_worker, url, mode, cell, requests=requests, barrier=barrier)
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(worker, range(concurrency)))
    samples = [sample for worker_samples in results for sample in worker_samples]
    latencies = sorted((end - start) * 1000.0 for start, end, _, _ in samples)
    wall = max(end for _, end, _, _ in samples) - min(start for start, _, _, _ in samples)
    kinds = Counter(kind for _, _, status, kind in samples if status != 200)
    return {
        "requests": len(samples),
        "errors": sum(kinds.values()),
        "error_kinds": dict(kinds),
        "wall_s": round(wall, 4),
        "rps": round(len(samples) / wall, 2) if wall > 0 else 0.0,
        "mean_ms": round(statistics.fmean(latencies), 3),
        "p50_ms": round(_percentile(latencies, 50), 3),
        "p95_ms": round(_percentile(latencies, 95), 3),
        "p99_ms": round(_percentile(latencies, 99), 3),
    }


def _start_sidecar(
    upstream_url: str,
    mode: str,
    *,
    blocks: tuple[ContextBlock, ...] = (),
    store: SqliteStore | None = None,
) -> tuple[ThreadingHTTPServer, str]:
    server = make_server(upstream=upstream_url, mode=mode, blocks=blocks, port=0, store=store)
    return server, _serve(server)


def run(
    *,
    concurrency: tuple[int, ...] = DEFAULT_CONCURRENCY,
    requests: int = DEFAULT_REQUESTS,
    blocks: int = DEFAULT_BLOCKS,
) -> dict[str, Any]:
    ts = time.strftime("%Y%m%d_%H%M%S")
    upstream = _FakeUpstreamServer(("127.0.0.1", 0), _FakeUpstream)
    upstream_url = _serve(upstream)
    servers: list[ThreadingHTTPServer] = [upstream]
    try:
        store = open_store(":memory:")
        store.append_many(_fabric(blocks))
        passthrough, passthrough_url = _start_sidecar(upstream_url, "passthrough")
        compile_server, compile_url = _start_sidecar(
            upstream_url, "compile", blocks=tuple(store.load_blocks()), store=store
        )
        servers += [passthrough, compile_server]

        modes: dict[str, dict[str, Any]] = {}
        cell = 0
        for mode, url in (("passthrough", passthrough_url), ("compile", compile_url)):
            httpx.post(
                f"{url}/v1/chat/completions",
                json={
                    "model": "bench",
                    "messages": [{"role": "user", "content": f"{mode} warmup"}],
                },
                timeout=30.0,
            )
            by_n: dict[str, Any] = {}
            for n in concurrency:
                cell += 1
                by_n[str(n)] = _phase(url, mode, cell, n, requests)
            modes[mode] = by_n

        overhead: dict[str, float] | None = None
        if 1 in concurrency:
            p1, c1 = modes["passthrough"]["1"], modes["compile"]["1"]
            overhead = {
                "p50_delta_ms": round(c1["p50_ms"] - p1["p50_ms"], 3),
                "mean_delta_ms": round(c1["mean_ms"] - p1["mean_ms"], 3),
            }

        report: dict[str, Any] = {
            "scenario": "bench_sidecar",
            "ts": ts,
            "config": {
                "concurrency": list(concurrency),
                "requests_per_client": requests,
                "fabric_blocks": blocks,
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "modes": modes,
            "overhead": overhead,
            "notes": [
                "Fake instant upstream: latency here is the sidecar's own cost (HTTP, "
                "proxying, compile+render, manifest-log write), not model inference time.",
                "Compile mode uses an in-memory SQLite store with manifest logging enabled; "
                "the write-lock path is exercised, disk-I/O noise is excluded.",
                "One process, one GIL: compile mode is CPU-bound and cannot scale past one "
                "core; this bounds the sidecar's concurrency cost, not deployment capacity.",
            ],
        }
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    return report


def _print(report: dict[str, Any]) -> None:
    config = report["config"]
    print(
        f"sidecar bench — {config['requests_per_client']} requests/client, "
        f"{config['fabric_blocks']} fabric blocks, python {config['python']}"
    )
    print(
        f"{'mode':<12}{'N':>4}{'reqs':>7}{'errors':>8}{'wall_s':>9}"
        f"{'req/s':>10}{'p50_ms':>9}{'p95_ms':>9}{'p99_ms':>9}"
    )
    for mode, by_n in report["modes"].items():
        for n, stats in by_n.items():
            print(
                f"{mode:<12}{n:>4}{stats['requests']:>7}{stats['errors']:>8}"
                f"{stats['wall_s']:>9.2f}{stats['rps']:>10.1f}{stats['p50_ms']:>9.2f}"
                f"{stats['p95_ms']:>9.2f}{stats['p99_ms']:>9.2f}"
            )
    overhead = report["overhead"]
    if overhead is not None:
        print(
            f"N=1 compile-minus-passthrough: p50 {overhead['p50_delta_ms']:+.2f} ms, "
            f"mean {overhead['mean_delta_ms']:+.2f} ms"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--concurrency",
        default="1,4,16,64",
        help="comma-separated concurrent client counts (default: 1,4,16,64)",
    )
    parser.add_argument(
        "--requests", type=int, default=DEFAULT_REQUESTS, help="requests per client"
    )
    parser.add_argument(
        "--blocks", type=int, default=DEFAULT_BLOCKS, help="fabric blocks for compile mode"
    )
    args = parser.parse_args()
    concurrency = tuple(int(part) for part in str(args.concurrency).split(","))
    report = run(concurrency=concurrency, requests=args.requests, blocks=args.blocks)
    out = REPORTS / f"bench_sidecar_{report['ts']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    _print(report)
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
