"""``cfx demo`` — productized scenarios that produce an evidence receipt.

``fleet``: six role agents answer short objectives on (A) a broadcast baseline that
pastes the entire fabric into every system message and (B) the sidecar's compile mode;
both arms share one deterministic scorer. ``negative``: a dead-end is written through
the real write path (``POST /fabric/blocks``) and bad-action recommendations are counted,
control vs treated. ``repo`` (``--repo PATH``): the before/after story on a real local
Python repository — the raw arm is the actual file bundle and the compile arm answers
objectives auto-derived from the repository's own AST symbols (``sidecar/repofixture.py``).
Everything runs in-process (temp SQLite fabric, sidecars on port 0) against a REAL
OpenAI-compatible upstream; no ``evals`` imports — product code stays standalone.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from kernel.cartridge.serializer import count_tokens
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
)
from sidecar.costs import PRESETS, estimate_cost, parse_price_spec
from sidecar.negscore import is_bad_answer, is_bad_answer_v2, score_v3
from sidecar.repofixture import build_repo_fixture, build_repo_store
from sidecar.server import make_server, open_store

# Deterministic SYNTHETIC "Project Atlas" project state (no real data): 10 core
# index-card blocks plus generated runbooks, incident logs, interface specs, and
# sprint digests. Sized so the broadcast blob is ~6k tokens (regression-tested).
# Core rows: block_id | block_kind | lifecycle_status | content
_CORE_ROWS = """\
arch|decision|routable|Architecture: canonical state in Postgres; embeddings in pgvector.
api|decision|routable|API contract: all Atlas services expose gRPC on port 7443.
auth|decision|routable|Auth: inter-service calls carry a short-lived Ed25519 token (max TTL 120s).
region|fact|routable|Primary region is eu-central-1; failover region is us-east-1.
staging|fact|routable|The Atlas staging cluster listens on port 9091.
limits|fact|routable|Rate limit for the public API is 480 requests per minute per tenant.
latency|observation|routable|Observed: p99 latency regressed to 820 ms after the June deploy.
nk-mirror|negative_knowledge|routable|Dead end: do NOT scale ingestion with mirror-poller mode.
nk-cron|negative_knowledge|routable|Dead end: do NOT run hourly full re-index jobs.
draft|summary|draft|Draft (unvalidated): rename the ledger table to atlas_ledger_v2.
"""

_SERVICES = ("ingest", "indexer", "gateway", "scheduler", "reconciler", "notifier")
_TEAMS = ("core", "platform", "sre")
_INCIDENTS = ("indexer restart storm", "gateway certificate rotation", "scheduler backlog spike")
_INTERFACES = (
    ("atlas-ingest", "POST /v1/events"),
    ("atlas-query", "GET /v1/query"),
    ("atlas-admin", "POST /v1/admin"),
    ("atlas-export", "GET /v1/export"),
)
_TICKETS = ("onboarding friction", "retention cleanup", "quota fairness")
_RUNBOOK_STEPS = 23
_INCIDENT_STEPS = 18
_MAX_TRAPS = 2


def _project_notes() -> tuple[tuple[str, BlockKind, str], ...]:
    """Deterministic filler documents (no RNG); keyword-clean w.r.t. traps/facts."""
    notes: list[tuple[str, BlockKind, str]] = []
    for i, service in enumerate(_SERVICES):
        lines = [
            f"# Runbook: atlas-{service} (v{2 + i % 4})",
            f"owner: team-{_TEAMS[i % 3]}; page the on-call after {10 + i * 5} minutes",
            "routine checks:",
        ]
        for step in range(1, _RUNBOOK_STEPS + 1):
            lines.append(
                f"{step:02d}. verify shard {step % 11} watermark lag under {5 + step % 30}s and "
                f"replay backlog on the {_TEAMS[(step + i) % 3]} board"
            )
        notes.append((f"runbook-{service}", BlockKind.artifact, "\n".join(lines)))
    for i, title in enumerate(_INCIDENTS):
        lines = [
            f"# Incident log: {title} (2026-0{3 + i}-1{7 + i})",
            f"severity: sev{1 + i % 2}; total duration: {25 + i * 7} minutes",
            "timeline:",
        ]
        for t in range(1, _INCIDENT_STEPS + 1):
            lines.append(
                f"T+{t * 5:03d}m: {_TEAMS[(t + i) % 3]} shed load from atlas-"
                f"{_SERVICES[(t + i) % len(_SERVICES)]} and confirmed recovery on the board"
            )
        notes.append((f"incident-{i + 1}", BlockKind.observation, "\n".join(lines)))
    for i, (name, route) in enumerate(_INTERFACES):
        lines = [
            f"# Interface spec: {name} -> {route}",
            "contract: v3; auth: short-lived tokens; pagination: cursor",
            "fields:",
        ]
        for f in range(1, 13):
            lines.append(f"- field_{f}: required, string, max {64 * f} chars, example v-{i}-{f}")
        notes.append((f"interface-{name}", BlockKind.fact, "\n".join(lines)))
    for i, theme in enumerate(_TICKETS):
        lines = [f"# Sprint {18 + i} digest: {theme}", "carry-over notes:"]
        for t in range(1, 21):
            lines.append(
                f"- ATL-{420 + i * 100 + t}: tighten {theme} path {t} in atlas-"
                f"{_SERVICES[(t + i) % len(_SERVICES)]}; owner team-{_TEAMS[t % 3]}"
            )
        notes.append((f"tickets-{18 + i}", BlockKind.summary, "\n".join(lines)))
    return tuple(notes)


# Fleet objectives: role | objective | required-substrings (comma-separated)
_FLEET_ROWS = """\
architect|Where does Project Atlas store canonical state and embeddings?|postgres,pgvector
implementer|Which port do Atlas services expose gRPC on? Give the port only.|7443
reviewer|What is the maximum TTL for inter-service auth tokens? Give seconds.|120
tester|What port does the Atlas staging cluster listen on? Give the port only.|9091
auditor|What was the p99 latency after the June deploy? Give ms.|820
ops|What is the Project Atlas API rate limit per tenant per minute?|480
"""


class FleetObjective(NamedTuple):
    role: str
    objective: str
    required: tuple[str, ...]


class TrapObjective(NamedTuple):
    trap_id: str
    objective: str
    bad_keywords: tuple[str, ...]
    dead_end: str


def _rows(table: str) -> list[list[str]]:
    return [line.split("|") for line in table.strip().splitlines()]


def fleet_workload() -> tuple[FleetObjective, ...]:
    """Six roles; each objective is answerable only from a specific fixture block."""
    return tuple(
        FleetObjective(role, objective, tuple(required.split(",")))
        for role, objective, required in _rows(_FLEET_ROWS)
    )


def negative_candidates() -> tuple[TrapObjective, ...]:
    """Candidate traps for the control-only pilot; each dead end is recorded ONLY here."""
    return (
        TrapObjective(
            "search",
            "We need full-text search across all Atlas blocks. Recommend one solution.",
            ("elasticsearch", "opensearch"),
            "Dead end: do NOT adopt Elasticsearch or OpenSearch for Atlas search; the cluster "
            "was abandoned in Q2 after ops cost tripled. Keep SQLite FTS5.",
        ),
        TrapObjective(
            "queue",
            "Atlas background jobs need a real task queue. What should we adopt?",
            ("celery", "redis"),
            "Dead end: do NOT adopt Celery or Redis queues for Atlas jobs; Redis lost 40k jobs "
            "in the May 2026 failover and was abandoned. Use the SQLite-backed queue.",
        ),
        TrapObjective(
            "cache",
            "Atlas API responses are slow. What caching layer should we add?",
            ("redis", "memcached"),
            "Dead end: do NOT add Redis or Memcached to Atlas; the 2026-02 pilot doubled ops "
            "load and was rolled back. Use the store's built-in row cache.",
        ),
        TrapObjective(
            "metrics",
            "We need observability for all Atlas services. What should we deploy?",
            ("prometheus", "grafana"),
            "Dead end: do NOT deploy Prometheus or Grafana for Atlas; the 2026-03 rollout "
            "filled the primary disk and was removed. Use the stats endpoint.",
        ),
        TrapObjective(
            "vector",
            "Atlas needs semantic search over past decisions. What should we adopt?",
            ("pinecone", "weaviate", "milvus"),
            "Dead end: do NOT adopt Pinecone, Weaviate, or Milvus for Atlas; the 2026-01 trial "
            "was dropped for latency. pgvector already covers this.",
        ),
    )


def build_fixture_store(path: Path) -> Path:
    """Create the deterministic synthetic Project Atlas fabric at ``path``."""
    store = SqliteStore(path)
    entries: list[tuple[str, BlockKind, str, LifecycleStatus]] = [
        (block_id, BlockKind(kind), content, LifecycleStatus(lifecycle))
        for block_id, kind, lifecycle, content in _rows(_CORE_ROWS)
    ]
    entries.extend(
        (block_id, kind, content, LifecycleStatus.routable)
        for block_id, kind, content in _project_notes()
    )
    fixture_as_of = datetime(2026, 1, 1, tzinfo=UTC)
    for seq, (block_id, kind, content, lifecycle) in enumerate(entries, start=1):
        store.append(
            ContextBlock(
                block_id=block_id,
                block_kind=kind,
                memory_tier=MemoryTier.ledger,
                content=content,
                content_hash="sha256:" + sha256_hex(content.encode("utf-8")),
                token_estimate=count_tokens(content),
                source_event_seq=seq,
                creator=Creator(type=CreatorType.agent, id="demo-fixture"),
                fidelity=Fidelity.extract,
                lifecycle_status=lifecycle,
                trust_domain="default",
                observed_at=fixture_as_of,
                valid_from=fixture_as_of,
            )
        )
    return path


def broadcast_text(blocks: Sequence[ContextBlock]) -> str:
    """The naive baseline's system message: entire fabric, stable order, plain text."""
    ordered = sorted(blocks, key=lambda b: (b.source_event_seq, b.block_id))
    lines = (f"[{b.block_kind.value}] {b.content or b.ref_uri or b.block_id}" for b in ordered)
    return "\n".join(lines)


def score_answer(text: str, required: Sequence[str]) -> tuple[bool, list[str]]:
    """Deterministic substring check; the same function for every arm."""
    lowered = text.lower()
    missing = [item for item in required if item.lower() not in lowered]
    return (not missing, missing)


# Scorers v1/v2/v3 live in ``sidecar/negscore.py`` (imported above; v3 pre-registered in
# docs/engineering/NEGATIVE-SCORER.md). Every negative-scenario response is scored by all
# three and every count is recorded.


class _Completion(NamedTuple):
    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    compiled: bool
    prompt_sha256: str


class _Counters(NamedTuple):
    hits: float | None
    queries: float | None
    kv: float | None


def _scrape_cache(client: httpx.Client, metrics_url: str) -> _Counters:
    """Best-effort vLLM /metrics counters; all-None when the upstream has none."""

    def prom_value(text: str, name: str) -> float | None:
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            if line.startswith(name + " ") or line.startswith(name + "{"):
                try:
                    value = float(line.rsplit(" ", 1)[1])
                except ValueError:
                    return None
                return value if math.isfinite(value) else None
        return None

    try:
        response = client.get(metrics_url, timeout=3.0)
    except httpx.HTTPError:
        return _Counters(None, None, None)
    if response.status_code != 200:
        return _Counters(None, None, None)
    text = response.text
    kv = prom_value(text, "vllm:kv_cache_usage_perc")
    if kv is None:
        kv = prom_value(text, "vllm:gpu_cache_usage_perc")
    return _Counters(
        prom_value(text, "vllm:prefix_cache_hits_total"),
        prom_value(text, "vllm:prefix_cache_queries_total"),
        kv,
    )


def _start_kv_sampler(
    client: httpx.Client, metrics_url: str, interval_s: float = 0.05
) -> Callable[[], float | None]:
    """Best-effort ~50ms sampler of the KV-cache gauge; returns a ``stop() -> peak``."""
    state: dict[str, float | None] = {"peak": None}
    stop = threading.Event()

    def sample() -> None:
        kv = _scrape_cache(client, metrics_url).kv
        if kv is not None and (state["peak"] is None or kv > state["peak"]):
            state["peak"] = kv

    def loop() -> None:
        while not stop.wait(interval_s):
            sample()

    sample()
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()

    def finish() -> float | None:
        stop.set()
        thread.join(timeout=2.0)
        return state["peak"]

    return finish


def _chat(
    client: httpx.Client,
    sidecar_url: str,
    *,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
) -> _Completion:
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


def _measure_arm(
    client: httpx.Client,
    url: str,
    metrics_url: str,
    messages: list[list[dict[str, str]]],
    *,
    model: str,
    max_tokens: int,
) -> tuple[list[_Completion], float, _Counters]:
    """Send every message list through one sidecar; collect completions, wall, cache."""
    before = _scrape_cache(client, metrics_url)
    stop_sampler = _start_kv_sampler(client, metrics_url)
    started = time.perf_counter()
    completions = [
        _chat(client, url, model=model, messages=message, max_tokens=max_tokens)
        for message in messages
    ]
    wall_ms = (time.perf_counter() - started) * 1000
    after = _scrape_cache(client, metrics_url)
    return (
        completions,
        wall_ms,
        _Counters(
            _delta(before.hits, after.hits),
            _delta(before.queries, after.queries),
            stop_sampler(),
        ),
    )


def _delta(before: float | None, after: float | None) -> float | None:
    if before is None or after is None:
        return None
    return after - before


def _price_per_mtok() -> float | None:
    raw = os.environ.get("CF_PRICE_INPUT_PER_MTOK")
    if not raw:
        return None
    try:
        price = float(raw)
    except ValueError:
        return None
    return price if math.isfinite(price) and price >= 0 else None


def _totals(records: list[dict[str, Any]], wall_ms: float) -> dict[str, Any]:
    prompt = sum(int(record["prompt_tokens"]) for record in records)
    summary: dict[str, Any] = {
        "requests": len(records),
        "prompt_tokens_total": prompt,
        "completion_tokens_total": sum(int(r["completion_tokens"]) for r in records),
        "wall_ms": round(wall_ms, 1),
        "mean_latency_ms": (
            round(sum(float(r["latency_ms"]) for r in records) / len(records), 1) or None
        ),
    }
    price = _price_per_mtok()
    if price is not None:
        summary["estimated_input_cost_usd"] = round(prompt * price / 1_000_000, 6)
    return summary


def _chat_record(
    op: str,
    arm: str,
    trap: TrapObjective,
    repeat: int,
    completion: _Completion,
    hits: list[str],
    hits_v2: list[str],
    evidence_v3: list[str],
) -> dict[str, Any]:
    return {
        "op": op,
        "arm": arm,
        "trap": trap.trap_id,
        "repeat": repeat,
        "objective": trap.objective,
        "bad": bool(hits),
        "bad_keywords_hit": hits,
        "bad_v2": bool(hits_v2),
        "bad_keywords_hit_v2": hits_v2,
        "bad_v3": bool(evidence_v3),
        "bad_evidence_v3": evidence_v3,
        "compiled": completion.compiled,
        "prompt_tokens": completion.prompt_tokens,
        "completion_tokens": completion.completion_tokens,
        "latency_ms": completion.latency_ms,
        "response": completion.text,
        "response_text": completion.text[:500],
        "prompt_sha256": completion.prompt_sha256,
    }


def _negative_arm(
    client: httpx.Client,
    arm: str,
    url: str,
    metrics_url: str,
    pairs: list[tuple[TrapObjective, int]],
    *,
    op: str,
    model: str,
    max_tokens: int,
) -> tuple[list[dict[str, Any]], float, _Counters]:
    messages = [[{"role": "user", "content": trap.objective}] for trap, _ in pairs]
    completions, wall_ms, counters = _measure_arm(
        client, url, metrics_url, messages, model=model, max_tokens=max_tokens
    )
    records: list[dict[str, Any]] = []
    for (trap, repeat), completion in zip(pairs, completions, strict=False):
        records.append(
            _chat_record(
                op,
                arm,
                trap,
                repeat,
                completion,
                is_bad_answer(completion.text, trap.bad_keywords),
                is_bad_answer_v2(completion.text, trap.bad_keywords),
                score_v3(completion.text, trap.bad_keywords).evidence,
            )
        )
    return records, wall_ms, counters


def _negative_summary(records: list[dict[str, Any]], wall_ms: float) -> dict[str, Any]:
    per_trap: dict[str, dict[str, int]] = {}
    for record in records:
        slot = per_trap.setdefault(record["trap"], {"bad": 0, "bad_v2": 0, "bad_v3": 0, "total": 0})
        slot["bad"] += int(record["bad"])
        slot["bad_v2"] += int(record["bad_v2"])
        slot["bad_v3"] += int(record["bad_v3"])
        slot["total"] += 1
    return {
        **_totals(records, wall_ms),
        "bad": sum(1 for record in records if record["bad"]),
        "bad_v2": sum(1 for record in records if record["bad_v2"]),
        "bad_v3": sum(1 for record in records if record["bad_v3"]),
        "total": len(records),
        "per_trap": per_trap,
    }


def _receipt(
    scenario: str,
    model: str,
    upstream: str,
    started_at: str,
    records: list[dict[str, Any]],
    arms: dict[str, dict[str, Any]],
    cache: dict[str, Any],
    notes: list[str],
) -> dict[str, Any]:
    return {
        "scenario": scenario,
        "model": model,
        "upstream": upstream,
        "started_at": started_at,
        "records": records,
        "arms": arms,
        "cache": cache,
        "notes": notes,
    }


def _cache_payload(per_arm: dict[str, _Counters]) -> dict[str, Any]:
    names = ("prefix_cache_hits_delta", "prefix_cache_queries_delta", "kv_cache_usage_peak")
    available = any(v is not None for c in per_arm.values() for v in c)
    return {
        "available": available,
        "source": "upstream Prometheus /metrics (vLLM)",
        "per_arm": {name: dict(zip(names, c, strict=True)) for name, c in per_arm.items()},
        "caveat": "Prefix-cache counters are upstream-global and vLLM-specific (null when the "
        "upstream exposes no /metrics); kv_cache_usage_peak is a best-effort ~50ms "
        "sample of the KV-cache capacity fraction, not comparable across models.",
    }


def _serve(server: ThreadingHTTPServer) -> str:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


def _stop(*servers: ThreadingHTTPServer) -> None:
    for server in servers:
        server.shutdown()
        server.server_close()


_MOCK_BANNER = "MOCK MODE — canned responses; accuracy is meaningless, this checks wiring/UX only"
_MOCK_NOTE = (
    "MOCK MODE: an in-process fake OpenAI endpoint returned canned responses (prompt tokens "
    "are the real token count of the messages sent; completion tokens are fixed small); "
    "accuracy is meaningless — this run checks wiring/UX only."
)
_MOCK_COMPLETION_TOKENS = 8
_MOCK_SAFE_ANSWER = "Use the built-in SQLite-backed alternative already shipped with Atlas."


def _mock_text(messages: Sequence[dict[str, Any]]) -> str:
    """Deterministic canned completion keyed by objective (fleet role or trap id)."""
    blob = "\n".join(str(message.get("content") or "") for message in messages)
    lowered = blob.lower()
    for spec in fleet_workload():
        if spec.objective.lower() in lowered:
            return f"{spec.role}: " + " ".join(spec.required)
    for trap in negative_candidates():
        if trap.objective.lower() not in lowered:
            continue
        if trap.dead_end.lower() in lowered:
            return _MOCK_SAFE_ANSWER
        return "Adopt " + " and ".join(trap.bad_keywords) + "."
    return _MOCK_SAFE_ANSWER


class _MockUpstream(BaseHTTPRequestHandler):
    """Minimal in-process OpenAI-compatible endpoint for ``cfx demo --mock``."""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            request: Any = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            request = {}
        raw_messages = request.get("messages") if isinstance(request, dict) else None
        messages = raw_messages if isinstance(raw_messages, list) else []
        text = _mock_text(messages)
        prompt_tokens = sum(
            count_tokens(str(message.get("content") or ""))
            for message in messages
            if isinstance(message, dict)
        )
        payload = {
            "id": "cmpl-mock",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": max(prompt_tokens, 1),
                "completion_tokens": _MOCK_COMPLETION_TOKENS,
            },
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


def _start_mock_upstream() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockUpstream)
    return server, _serve(server)


def _cached_tokens(prompt_tokens: int, counters: _Counters | None) -> int:
    """Estimate cached prompt tokens from vLLM's request-level prefix-cache ratio."""
    if counters is None or counters.hits is None or counters.queries is None:
        return 0
    if counters.queries <= 0:
        return 0
    ratio = min(max(counters.hits / counters.queries, 0.0), 1.0)
    return round(prompt_tokens * ratio)


def _cost_section(
    price: str, arms: dict[str, dict[str, Any]], per_arm_cache: dict[str, _Counters]
) -> dict[str, Any]:
    preset, custom = parse_price_spec(price)
    per_arm: dict[str, dict[str, Any]] = {}
    cached_seen = False
    for name, arm in arms.items():
        prompt = int(arm["prompt_tokens_total"])
        completion = int(arm["completion_tokens_total"])
        cached = _cached_tokens(prompt, per_arm_cache.get(name))
        cached_seen = cached_seen or cached > 0
        breakdown = estimate_cost(
            preset,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cached_tokens=cached,
            custom=custom,
        )
        assert breakdown is not None
        requests = int(arm["requests"])
        per_arm[name] = {
            "requests": requests,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cached_tokens": breakdown.cached_tokens,
            "input_usd": round(breakdown.input_usd, 8),
            "cached_input_usd": round(breakdown.cached_input_usd, 8),
            "cache_write_usd": round(breakdown.cache_write_usd, 8),
            "output_usd": round(breakdown.output_usd, 8),
            "total_usd": round(breakdown.total_usd, 8),
            "per_request_usd": round(breakdown.total_usd / requests, 8) if requests else 0.0,
        }
    assumptions: list[str] = []
    if preset is not None:
        entry = PRESETS[preset]
        assumptions.append(
            f"{preset} list prices checked {entry['checked']} ({entry['source']}); "
            "providers change prices — verify before relying on this."
        )
    else:
        assumptions.append(
            "custom rates supplied on the command line (USD per 1M tokens); no published "
            "source attached."
        )
    if cached_seen:
        assumptions.append(
            "cached discount estimated from vLLM prefix-cache hits/queries (request-level "
            "counters applied proportionally to prompt tokens); cache writes not modeled."
        )
    else:
        assumptions.append(
            "no usable cached-token counters; all input tokens billed at full price "
            "(cache writes not modeled)."
        )
    assumptions.append(
        "cost model is token-count × published list prices; provider billing quirks "
        "(Anthropic cache TTLs, minimums) are not modeled."
    )
    section: dict[str, Any] = {
        "preset": preset if preset is not None else "custom",
        "per_arm": per_arm,
        "assumptions": assumptions,
    }
    baseline = "broadcast" if "broadcast" in per_arm else "raw" if "raw" in per_arm else None
    if baseline is not None and "compile" in per_arm:
        before = float(per_arm[baseline]["total_usd"])
        after = float(per_arm["compile"]["total_usd"])
        section["savings_pct"] = round((before - after) / before * 100, 1) if before > 0 else None
    return section


def run_fleet(
    client: httpx.Client,
    upstream: str,
    *,
    model: str,
    max_tokens: int = 96,
    tmp_dir: Path,
    price: str | None = None,
) -> dict[str, Any]:
    """Broadcast-vs-compile A/B over the fleet workload; returns the receipt dict."""
    if price is not None:
        parse_price_spec(price)
    started_at = datetime.now(tz=UTC).isoformat()
    build_fixture_store(tmp_dir / "fleet.db")
    compile_store = open_store(tmp_dir / "fleet.db")
    blocks = tuple(compile_store.load_blocks())
    blob = broadcast_text(blocks)
    servers = {
        "broadcast": make_server(upstream=upstream, mode="passthrough", port=0),
        "compile": make_server(
            upstream=upstream, mode="compile", blocks=blocks, port=0, store=compile_store
        ),
    }
    urls = {arm: _serve(server) for arm, server in servers.items()}
    metrics_url = upstream.rstrip("/") + "/metrics"
    objectives = fleet_workload()
    records: list[dict[str, Any]] = []
    arms: dict[str, dict[str, Any]] = {}
    per_arm_cache: dict[str, _Counters] = {}
    try:
        for arm, url in urls.items():
            messages = [
                [{"role": "system", "content": blob}, {"role": "user", "content": spec.objective}]
                if arm == "broadcast"
                else [{"role": "user", "content": spec.objective}]
                for spec in objectives
            ]
            completions, wall_ms, per_arm_cache[arm] = _measure_arm(
                client, url, metrics_url, messages, model=model, max_tokens=max_tokens
            )
            arm_records: list[dict[str, Any]] = []
            for spec, completion in zip(objectives, completions, strict=False):
                passed, missing = score_answer(completion.text, spec.required)
                arm_records.append(
                    {
                        "op": "chat",
                        "arm": arm,
                        "agent": spec.role,
                        "objective": spec.objective,
                        "required": list(spec.required),
                        "missing": missing,
                        "passed": passed,
                        "compiled": completion.compiled,
                        "prompt_tokens": completion.prompt_tokens,
                        "completion_tokens": completion.completion_tokens,
                        "latency_ms": completion.latency_ms,
                        "response": completion.text,
                        "response_text": completion.text[:500],
                        "prompt_sha256": completion.prompt_sha256,
                    }
                )
            records.extend(arm_records)
            arms[arm] = {
                **_totals(arm_records, wall_ms),
                "passed": sum(1 for r in arm_records if r["passed"]),
                "total": len(arm_records),
            }
    finally:
        _stop(*servers.values())
    receipt: dict[str, Any] = {
        "scenario": "fleet",
        "model": model,
        "upstream": upstream,
        "started_at": started_at,
        "records": records,
        "arms": arms,
        "cache": _cache_payload(per_arm_cache),
        "notes": [
            "Fixture is synthetic Project Atlas state (generated in `sidecar/demo.py`; no real "
            "project data).",
            "Broadcast is deliberately the strongest naive baseline: one identical, "
            "stable-order fabric blob in every system message, maximizing prefix-cache reuse.",
            "Accuracy is a deterministic substring check (no LLM judge) proving information "
            "parity, not answer quality; both arms use the exact same scorer.",
            "Broadcast ran first; the arms use different system prefixes so upstream "
            "prefix-cache state cannot leak between them.",
            "Prefix-cache counters are warm-cache numbers: the upstream cache persists across "
            "requests and runs, so prompts already sent earlier in the same session (including "
            "a smoke run with identical messages) show up as hits; cache deltas are not a "
            "cold-start measurement.",
            f"Small n: {len(objectives)} objectives x 1 repeat; temperature=0.0 requested, "
            f"max_tokens={max_tokens}, non-streaming.",
            f"Broadcast renders all stored blocks ({count_tokens(blob)} tokens) including the "
            "quarantined draft (the baseline applies no policy); compiled packets deny the "
            "draft (lifecycle:draft) and include only relevant blocks.",
            "Each chat record carries the full `response` plus `response_text` (first 500 "
            "chars) and `prompt_sha256`, the sha256 of the messages sent by the client "
            "(pre-injection; in compile mode the system message is rendered server-side and "
            "is not part of this hash).",
        ],
    }
    if price is not None:
        receipt["cost"] = _cost_section(price, arms, per_arm_cache)
    return receipt


def run_repo(
    client: httpx.Client,
    upstream: str,
    *,
    model: str,
    repo: Path,
    max_tokens: int = 96,
    tmp_dir: Path,
    price: str | None = None,
) -> dict[str, Any]:
    """Raw-file-bundle vs compiled-packet A/B on an auto-built repo fixture (``--repo``)."""
    if price is not None:
        parse_price_spec(price)
    started_at = datetime.now(tz=UTC).isoformat()
    fixture = build_repo_fixture(repo)
    build_repo_store(fixture, tmp_dir / "repo.db")
    compile_store = open_store(tmp_dir / "repo.db")
    blocks = tuple(compile_store.load_blocks())
    servers = {
        "raw": make_server(upstream=upstream, mode="passthrough", port=0),
        "compile": make_server(
            upstream=upstream, mode="compile", blocks=blocks, port=0, store=compile_store
        ),
    }
    urls = {arm: _serve(server) for arm, server in servers.items()}
    metrics_url = upstream.rstrip("/") + "/metrics"
    objectives = fixture.objectives
    records: list[dict[str, Any]] = []
    arms: dict[str, dict[str, Any]] = {}
    per_arm_cache: dict[str, _Counters] = {}
    try:
        for arm, url in urls.items():
            messages = [
                [
                    {"role": "system", "content": fixture.bundle},
                    {"role": "user", "content": spec.objective},
                ]
                if arm == "raw"
                else [{"role": "user", "content": spec.objective}]
                for spec in objectives
            ]
            completions, wall_ms, per_arm_cache[arm] = _measure_arm(
                client, url, metrics_url, messages, model=model, max_tokens=max_tokens
            )
            arm_records: list[dict[str, Any]] = []
            for spec, completion in zip(objectives, completions, strict=False):
                passed, missing = score_answer(completion.text, spec.keys)
                arm_records.append(
                    {
                        "op": "chat",
                        "arm": arm,
                        "agent": spec.role,
                        "objective": spec.objective,
                        "required": list(spec.keys),
                        "missing": missing,
                        "passed": passed,
                        "compiled": completion.compiled,
                        "prompt_tokens": completion.prompt_tokens,
                        "completion_tokens": completion.completion_tokens,
                        "latency_ms": completion.latency_ms,
                        "response": completion.text,
                        "response_text": completion.text[:500],
                        "prompt_sha256": completion.prompt_sha256,
                    }
                )
            records.extend(arm_records)
            arms[arm] = {
                **_totals(arm_records, wall_ms),
                "passed": sum(1 for r in arm_records if r["passed"]),
                "total": len(arm_records),
            }
    finally:
        _stop(*servers.values())
    receipt: dict[str, Any] = {
        "scenario": "repo",
        "model": model,
        "upstream": upstream,
        "started_at": started_at,
        "fixture": {
            "kind": "repo",
            "path": str(repo),
            "files": len(fixture.files),
            "raw_tokens": fixture.raw_tokens,
        },
        "records": records,
        "arms": arms,
        "cache": _cache_payload(per_arm_cache),
        "notes": [
            f"Fixture is REAL repository content: {len(fixture.files)} Python files "
            f"({fixture.raw_tokens} o200k tokens) selected deterministically from {repo} "
            "(sorted relative paths; .git/.venv/venv/site-packages/__pycache__/tests/build/"
            "dist/caches/node_modules skipped; files over 20000 tokens skipped); the raw "
            "arm's system message is the actual file bytes with `===== FILE:` headers in "
            "stable order.",
            "Objectives are derived automatically from the real AST symbol data (no "
            f"hand-written workload): {len(objectives)} roles each ask in which file a "
            "distinct symbol is defined, and the answer key is the real relative file "
            "path, scored as a case-insensitive substring of the response for both arms "
            "(information presence, not answer quality).",
            f"Compile arm = the shipped sidecar's compile mode over an in-memory SQLite "
            f"fabric of {len(fixture.blocks) - 1} fact blocks built from real module "
            "docstring lines and symbol signatures, plus the Tier-0 .cfx cartridge hologram "
            f"({fixture.cartridge.cartridge_id}, {fixture.cartridge.token_count} tokens) "
            "stored as a routable artifact block; the shipped server's compile path takes "
            "no cartridge argument, so the hologram enters through the fabric, not as a "
            "tier-0 pin.",
            "Raw ran first; all raw requests share one identical system message (the "
            "strongest naive baseline, maximizing upstream prefix-cache reuse); cache "
            "deltas are warm-cache numbers.",
            "Each chat record carries the full `response` plus `response_text` (first 500 "
            "chars) and `prompt_sha256`, the sha256 of the messages the client sent "
            "(pre-injection; in compile mode the system message is rendered server-side "
            "and is not part of this hash).",
        ],
    }
    if price is not None:
        receipt["cost"] = _cost_section(price, arms, per_arm_cache)
    return receipt


def run_negative(
    client: httpx.Client,
    upstream: str,
    *,
    model: str,
    max_tokens: int = 96,
    tmp_dir: Path,
    price: str | None = None,
    freeze_traps: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Pilot trap candidates control-only, then measure the frozen traps control-vs-treated.

    With ``freeze_traps`` (replication runs) the pilot is skipped and exactly the named
    traps are measured, so a rerun stays comparable with the set it replicates.
    """
    if price is not None:
        parse_price_spec(price)
    started_at = datetime.now(tz=UTC).isoformat()
    repeats = 2
    build_fixture_store(tmp_dir / "control.db")
    build_fixture_store(tmp_dir / "treated.db")
    control_store = open_store(tmp_dir / "control.db")
    treated_store = open_store(tmp_dir / "treated.db")
    control_server = make_server(
        upstream=upstream,
        mode="compile",
        blocks=tuple(control_store.load_blocks()),
        port=0,
        store=control_store,
    )
    control_url = _serve(control_server)
    treated_server: ThreadingHTTPServer | None = None
    metrics_url = upstream.rstrip("/") + "/metrics"
    candidates = negative_candidates()
    records: list[dict[str, Any]] = []
    arms: dict[str, dict[str, Any]] = {}
    per_arm_cache: dict[str, _Counters] = {}
    notes = [
        "Fixture is synthetic Project Atlas state (generated in `sidecar/demo.py`; no real "
        "project data); treated dead ends are written through the real write path "
        "(POST /fabric/blocks; the treated server runs with trust_blocks so dead ends are "
        "routable).",
        "Composition: control fabric = the synthetic fixture only (its two negative_knowledge "
        "blocks are unrelated to the traps); treated fabric = the same fixture plus the frozen "
        "traps' dead ends (see write records).",
        "Scorers: every response is scored by v1 (mentioned-keyword substring, unchanged), v2 "
        "(affirmative-recommendation window rule; see docs/engineering/DEMO.md), and v3 "
        "(clause-scoped recommendation rule, pre-registered in "
        "docs/engineering/NEGATIVE-SCORER.md); arms report bad (v1), bad_v2, and bad_v3. v2 "
        "was authored after observing v1 outputs of the 2026-10-17 _211536/_212021 runs — an "
        "exploratory instrument fix applied symmetrically, not pre-registered before the first "
        "run; v3 was pre-registered before its code existed.",
        "Prohibition presence: treated prompts end with the PROHIBITIONS section (recency "
        "anchor, rendered since the 2026-10-17 renderer fix); control prompts end with the "
        "fixture's unrelated prohibitions only.",
        "Each chat record carries the full `response` plus `response_text` (first 500 chars) "
        "and `prompt_sha256`, the sha256 of the messages sent by the client (pre-injection; "
        "in compile mode the system message is rendered server-side and is not part of this "
        "hash).",
    ]
    chosen: list[TrapObjective] = []
    if freeze_traps is None:
        notes.append(
            f"Pilot protocol: candidates are piloted on the CONTROL arm only (never treated), "
            f"{repeats} repeats each; a candidate is kept when its control bad count is non-zero, "
            "and the first two kept candidates are frozen for the final measurement."
        )
        notes.append(
            "Cache counter caveat: the pilot sends the same prompts as the final control arm, so "
            "control's cache deltas are warm-cache numbers (pilot requests are excluded from the "
            "deltas but not from the upstream cache)."
        )
    else:
        by_id = {candidate.trap_id: candidate for candidate in candidates}
        unknown = [trap_id for trap_id in freeze_traps if trap_id not in by_id]
        if unknown:
            raise ValueError(f"unknown trap id(s): {', '.join(unknown)}")
        chosen = [by_id[trap_id] for trap_id in freeze_traps]
        notes.append(
            "Replication protocol: traps supplied via --freeze-traps; the control-only pilot "
            "is SKIPPED (no re-selection), keeping the frozen set comparable with the set "
            "being replicated. With no pilot pre-warm, control cache deltas are cold-ish."
        )
    try:
        if freeze_traps is None:
            pilot_pairs = [
                (candidate, repeat) for candidate in candidates for repeat in range(repeats)
            ]
            pilot_records, _, _ = _negative_arm(
                client,
                "control",
                control_url,
                metrics_url,
                pilot_pairs,
                op="pilot",
                model=model,
                max_tokens=max_tokens,
            )
            records.extend(pilot_records)
            pilot_counts: dict[str, int] = {}
            for record in pilot_records:
                pilot_counts[record["trap"]] = pilot_counts.get(record["trap"], 0) + int(
                    record["bad"]
                )
            notes.append(
                "Pilot (control-only, bad counts): "
                + "; ".join(
                    f"{c.trap_id} {pilot_counts.get(c.trap_id, 0)}/{repeats}" for c in candidates
                )
            )
            chosen = [c for c in candidates if pilot_counts.get(c.trap_id, 0) > 0][:_MAX_TRAPS]
            if not chosen:
                notes.append(
                    "NULL RESULT: no candidate produced a bad-action answer in the control pilot "
                    "(see op=pilot records); no treated measurement was run, so no effect is "
                    "claimed. A discriminating trap would need a larger model or a bad action "
                    "that matches the model's prior more strongly."
                )
                receipt = _receipt(
                    "negative",
                    model,
                    upstream,
                    started_at,
                    records,
                    arms,
                    _cache_payload({}),
                    notes,
                )
                if price is not None:
                    receipt["cost"] = _cost_section(price, arms, per_arm_cache)
                return receipt
            notes.append(
                f"Frozen traps: {', '.join(c.trap_id for c in chosen)} "
                f"({len(chosen)} trap(s) x {repeats} repeats per arm for the final run; pilot "
                "requests are excluded from the cache counters)."
            )
        final_pairs = [(candidate, repeat) for candidate in chosen for repeat in range(repeats)]
        control_records, wall_ms, per_arm_cache["control"] = _negative_arm(
            client,
            "control",
            control_url,
            metrics_url,
            final_pairs,
            op="chat",
            model=model,
            max_tokens=max_tokens,
        )
        records.extend(control_records)
        arms["control"] = _negative_summary(control_records, wall_ms)
        treated_server = make_server(
            upstream=upstream,
            mode="compile",
            blocks=tuple(treated_store.load_blocks()),
            port=0,
            store=treated_store,
            trust_blocks=True,
        )
        treated_url = _serve(treated_server)
        for candidate in chosen:
            response = client.post(
                f"{treated_url}/fabric/blocks",
                json={
                    "block_kind": "negative_knowledge",
                    "content": candidate.dead_end,
                },
            )
            response.raise_for_status()
            data = response.json()
            records.append(
                {
                    "op": "write",
                    "arm": "treated",
                    "status": response.status_code,
                    "block_id": data["block_id"],
                    "lifecycle_status": data["lifecycle_status"],
                    "source_event_seq": data["source_event_seq"],
                }
            )
        treated_records, wall_ms, per_arm_cache["treated"] = _negative_arm(
            client,
            "treated",
            treated_url,
            metrics_url,
            final_pairs,
            op="chat",
            model=model,
            max_tokens=max_tokens,
        )
        records.extend(treated_records)
        arms["treated"] = _negative_summary(treated_records, wall_ms)
    finally:
        if treated_server is not None:
            _stop(treated_server)
        _stop(control_server)
    receipt = _receipt(
        "negative",
        model,
        upstream,
        started_at,
        records,
        arms,
        _cache_payload(per_arm_cache),
        notes,
    )
    if price is not None:
        receipt["cost"] = _cost_section(price, arms, per_arm_cache)
    return receipt


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--upstream",
        default=os.environ.get("CF_UPSTREAM", "http://127.0.0.1:8000"),
        help="REAL OpenAI-compatible upstream base URL (env: CF_UPSTREAM)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("CF_MODEL", "default"),
        help="model name to request upstream (env: CF_MODEL)",
    )
    parser.add_argument(
        "--scenario",
        choices=("fleet", "negative"),
        default="fleet",
        help="fleet: broadcast-vs-compile A/B; negative: control-vs-treated dead-end",
    )
    parser.add_argument("--out", default="demo_receipt.json", help="receipt JSON output path")
    parser.add_argument("--max-tokens", type=int, default=96, help="max_tokens per request")
    parser.add_argument("--timeout", type=float, default=120.0, help="per-request timeout seconds")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="no upstream required: run an in-process fake OpenAI endpoint with canned responses",
    )
    parser.add_argument(
        "--price",
        default=None,
        help="price preset (e.g. openai:gpt-4o-mini) or custom:IN,OUT,CACHED[,WRITE] "
        "in USD per 1M tokens",
    )
    parser.add_argument(
        "--freeze-traps",
        default=None,
        metavar="ID[,ID...]",
        help="negative scenario only: skip the control-only pilot and measure exactly these "
        "trap ids (replication runs; see docs/engineering/DEMO.md)",
    )
    parser.add_argument(
        "--repo",
        default=None,
        metavar="PATH",
        help="run the repo scenario on a local Python repository instead of the synthetic "
        "fixture: raw file bundle vs compiled packets, objectives auto-derived from real "
        "symbols (fleet-style; incompatible with --scenario negative)",
    )


def _print_receipt(receipt: dict[str, Any]) -> None:
    if receipt.get("mock"):
        print(_MOCK_BANNER)
    print(f"scenario: {receipt['scenario']}  model: {receipt['model']}")
    print(f"upstream: {receipt['upstream']}")
    fixture = receipt.get("fixture")
    if fixture:
        print(
            f"fixture: {fixture['kind']} {fixture['path']} "
            f"({fixture['files']} files, {fixture['raw_tokens']} raw tokens)"
        )
    print(f"{'arm':<10}{'reqs':>5}{'prompt_tok':>11}{'compl_tok':>10}{'mean_s':>8}  result")
    for name, arm in receipt["arms"].items():
        result = (
            f"acc {arm['passed']}/{arm['total']}"
            if "passed" in arm
            else f"bad {arm['bad']}/{arm['total']} v2 {arm['bad_v2']}/{arm['total']} "
            f"v3 {arm['bad_v3']}/{arm['total']}"
        )
        mean_s = float(arm.get("mean_latency_ms") or 0.0) / 1000
        print(
            f"{name:<10}{arm['requests']:>5}{arm['prompt_tokens_total']:>11}"
            f"{arm['completion_tokens_total']:>10}{mean_s:>8.2f}  {result}"
        )
    if receipt["cache"]["available"]:
        for name, stats in receipt["cache"]["per_arm"].items():
            print(
                f"cache[{name}]: hits+={stats['prefix_cache_hits_delta']} "
                f"queries+={stats['prefix_cache_queries_delta']} "
                f"kv_peak={stats['kv_cache_usage_peak']}"
            )
    else:
        print("cache: unavailable (upstream has no /metrics) - counters skipped")
    cost = receipt.get("cost")
    if cost:
        for name, arm_cost in cost["per_arm"].items():
            print(
                f"cost[{name}] {cost['preset']}: ${arm_cost['total_usd']:.6f} total, "
                f"${arm_cost['per_request_usd']:.6f}/req"
            )
        if "savings_pct" in cost:
            baseline = "broadcast" if "broadcast" in cost["per_arm"] else "raw"
            print(f"cost: savings {cost['savings_pct']}% (compile vs {baseline})")
        for line in cost["assumptions"]:
            print(f"cost note: {line}")


def main_from_namespace(args: argparse.Namespace) -> int:
    if args.max_tokens <= 0 or args.timeout <= 0:
        print("cfx demo: --max-tokens and --timeout must be positive", file=sys.stderr)
        return 2
    if args.price is not None:
        try:
            parse_price_spec(args.price)
        except ValueError as exc:
            print(f"cfx demo: {exc}", file=sys.stderr)
            return 2
    freeze: list[str] | None = None
    if args.freeze_traps is not None:
        if args.scenario != "negative":
            print("cfx demo: --freeze-traps requires --scenario negative", file=sys.stderr)
            return 2
        known = [candidate.trap_id for candidate in negative_candidates()]
        freeze = [part.strip() for part in args.freeze_traps.split(",") if part.strip()]
        unknown = [trap_id for trap_id in freeze if trap_id not in known]
        if not freeze or unknown:
            label = ", ".join(unknown) if unknown else args.freeze_traps
            print(f"cfx demo: unknown trap id(s): {label}", file=sys.stderr)
            return 2
    repo: Path | None = None
    if args.repo is not None:
        if args.scenario != "fleet":
            print(
                "cfx demo: --repo runs the repo scenario and cannot be combined with "
                "--scenario negative",
                file=sys.stderr,
            )
            return 2
        repo = Path(args.repo)
        if not repo.is_dir():
            print(f"cfx demo: --repo {repo} is not a directory", file=sys.stderr)
            return 2
    mock_server: ThreadingHTTPServer | None = None
    upstream = args.upstream
    if args.mock:
        mock_server, upstream = _start_mock_upstream()
    try:
        with tempfile.TemporaryDirectory(prefix="cfx_demo_", ignore_cleanup_errors=True) as tmp:
            with httpx.Client(timeout=args.timeout, trust_env=False) as client:
                try:
                    if repo is not None:
                        receipt = run_repo(
                            client,
                            upstream,
                            model=args.model,
                            repo=repo,
                            max_tokens=args.max_tokens,
                            tmp_dir=Path(tmp),
                            price=args.price,
                        )
                    elif args.scenario == "negative":
                        receipt = run_negative(
                            client,
                            upstream,
                            model=args.model,
                            max_tokens=args.max_tokens,
                            tmp_dir=Path(tmp),
                            price=args.price,
                            freeze_traps=freeze,
                        )
                    else:
                        receipt = run_fleet(
                            client,
                            upstream,
                            model=args.model,
                            max_tokens=args.max_tokens,
                            tmp_dir=Path(tmp),
                            price=args.price,
                        )
                except ValueError as exc:
                    print(f"cfx demo: {exc}", file=sys.stderr)
                    return 2
                except httpx.HTTPError as exc:
                    print(f"cfx demo: request failed: {exc}", file=sys.stderr)
                    return 2
    finally:
        if mock_server is not None:
            _stop(mock_server)
    if args.mock:
        receipt["mock"] = True
        receipt["notes"].insert(0, _MOCK_NOTE)
    out = Path(args.out)
    try:
        out.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"cfx demo: cannot write {out}: {exc}", file=sys.stderr)
        return 2
    _print_receipt(receipt)
    print(f"receipt: {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sidecar.demo",
        description="Context Fabric demo: fleet A/B and negative-knowledge receipts.",
    )
    add_arguments(parser)
    return main_from_namespace(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
