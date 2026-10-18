"""Deterministic recall benchmark — lexical (naive overlap) vs BM25 vs embeddings vs hybrid.

Builds a labeled synthetic retrieval set by construction (no RNG, fixed strings):
20 topics x 6 notes = 120 blocks. Per topic: 2 relevant notes, 2 near-duplicate
distractors (share 2 of the query's content tokens but describe another subsystem), and
2 keyword-stuffed negatives (contain the *full* query string in a long low-density
digest line). Three topics are paraphrase queries whose relevant notes share no literal
token with the query, so only a semantic model can retrieve them.

Metrics per mode: precision@5, recall@10, MRR, top-1 accuracy (macro-averaged over
queries). Ties in ranking break by block_id (deterministic). Writes
``evals/reports/recall_bench_<utc-timestamp>.json`` with per-query rows + aggregates.

Run:  uv run --extra embeddings python -m evals.recall_bench
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kernel.cartridge.serializer import count_tokens
from kernel.router import embeddings
from kernel.router.recall import bm25_scores
from protocol.models import (
    BlockKind,
    ContextBlock,
    Creator,
    CreatorType,
    Fidelity,
    MemoryTier,
)

REPORTS = Path(__file__).parent / "reports"

_OLD_WORD = re.compile(r"[a-z0-9_]+")  # pre-BM25 tokenizer, kept for the baseline

FILLER = (
    "The weekly sync moved to Thursday and the agenda template was refreshed.",
    "Badge access for the east wing was rotated on schedule this week.",
    "The coffee machine on floor three is still awaiting a replacement valve.",
    "Parking passes for contractors are renewed at the front desk each quarter.",
    "The town hall slides are archived and the recording link expires soon.",
    "Office plants were watered on Tuesday and the sensor battery was replaced.",
)


@dataclass(frozen=True)
class Topic:
    tag: str
    query: str
    rel_a: str
    rel_b: str
    dup_a: str
    dup_b: str


TOPICS: tuple[Topic, ...] = (
    Topic(
        "retry-backoff",
        "retry backoff overflow",
        "Retry backoff overflow occurs when the multiplier bypasses the attempt cap; clamp the backoff at thirty seconds.",
        "The retry backoff starts at 200ms and doubles per attempt; the backoff overflow guard drops excess jitter.",
        "Retry backoff notes moved to the scheduler guide; the burst cap links the jitter annex.",
        "Backoff overflow incidents are owned by the scheduler rotation; see the incident template.",
    ),
    Topic(
        "token-budget",
        "token budget allocation",
        "Token budget allocation reserves 600 tokens for the cartridge and rations the remainder across tiers.",
        "The token budget allocation is computed bottom-up; allocation never exceeds the hard total.",
        "Token budget notes moved to the packing guide; the reserve section links the malloc annex.",
        "Budget allocation incidents are owned by the packing rotation; see the incident template.",
    ),
    Topic(
        "hnsw-index",
        "hnsw index recall",
        "HNSW index recall degrades when the ef_search parameter is too low; raise it for recall over speed.",
        "The hnsw index is built once per tenant; index recall was measured at 0.93 on the labeled set.",
        "Index recall notes moved to the vector guide; the ef_search section links the tuning annex.",
        "Hnsw recall incidents are owned by the search rotation; see the incident template.",
    ),
    Topic(
        "epoch-fold",
        "epoch fold append",
        "Epoch fold batches appends into micro-epochs; a fold closes as soon as the append window drains.",
        "The epoch fold is append-only; fold replay reads appends in sequence and never rewrites.",
        "Epoch notes moved to the lifecycle guide; the append window links the drain annex.",
        "Fold appends are owned by the lifecycle rotation; see the incident template.",
    ),
    Topic(
        "packet-hash",
        "packet content hash",
        "The packet content hash covers every tier byte-for-byte; a hash mismatch fails verification.",
        "Packet hashing is canonical: the content hash is stable across platforms and key orderings.",
        "Packet notes moved to the signing guide; the hash section links the canonicalization annex.",
        "Content hash checks are owned by the release rotation; see the packet template.",
    ),
    Topic(
        "deny-default",
        "policy deny default",
        "The policy engine denies by default; a deny decision requires an explicit allow rule match.",
        "Deny-by-default means missing policy metadata is a denial, not a silent allow.",
        "Policy notes moved to the trust guide; the deny section links the rbac annex.",
        "Default allow rules are owned by the security rotation; see the policy template.",
    ),
    Topic(
        "watermark",
        "watermark event sequence",
        "The watermark event sequence pins every included block to an event seq at or before the watermark.",
        "Watermark exclusion drops blocks with a source event sequence beyond the pinned watermark.",
        "Watermark notes moved to the epoch guide; the sequence section links the pinning annex.",
        "Event sequence checks gate ingestion batches before they reach the fold.",
    ),
    Topic(
        "cartridge-prefix",
        "cartridge prefix cache",
        "The cartridge prefix cache is keyed by the kv prefix hash; a cache miss rebuilds the hologram.",
        "Prefix cache reuse for the cartridge avoids re-prefill; cache entries pin the same kv spans.",
        "Cartridge notes moved to the serializer guide; the prefix section links the kv annex.",
        "Cache prefix spans are owned by the serving rotation and evict on watermark drift.",
    ),
    Topic(
        "mmr-packing",
        "mmr packing diversity",
        "MMR packing trades relevance for diversity; packing stops when the marginal gain flattens.",
        "The mmr packing pass runs after allocation; diversity is measured against already-packed blocks.",
        "Packing notes moved to the allocation guide; the diversity section links the lambda annex.",
        "Diversity packing ratios are owned by the serving rotation and tuned per stage.",
    ),
    Topic(
        "u-curve",
        "u-curve anchor placement",
        "The u-curve anchor places the instruction at the head and the recency anchor at the tail.",
        "U curve placement exploits attention wells; the anchor blocks sit at both context ends.",
        "Curve notes moved to the layout guide; the anchor section links the attention annex.",
        "Anchor placement rules are owned by the layout rotation and cap the tail reserve.",
    ),
    Topic(
        "nk-closure",
        "negative knowledge closure",
        "Negative knowledge closure force-includes the failure note whenever its subject block is selected.",
        "The closure pass pins negative knowledge blocks; closure never drops a matching prohibition.",
        "Knowledge notes moved to the dependency guide; the negative section links the prohibition annex.",
        "Negative closure of rules is owned by the publish rotation; deltas stay out.",
    ),
    Topic(
        "dep-closure",
        "dependency closure parent",
        "Dependency closure walks depends_on edges and force-includes parent blocks of any seed.",
        "The dependency closure is transitive; a parent chain is always included with its child.",
        "Dependency notes moved to the topology guide; the parent section links the edge annex.",
        "Closure of parent chains is owned by the router rotation and bounds the walk depth.",
    ),
    Topic(
        "shadow-probe",
        "shadow probe verification",
        "Shadow probe verification runs a canary query against the packed context before release.",
        "Each shadow probe verifies the packet answers its own objective; failed verification blocks release.",
        "Probe notes moved to the release guide; the verification section links the canary annex.",
        "Verification of shadow canaries is owned by the release rotation and runs per packet.",
    ),
    Topic(
        "trust-domain",
        "trust domain isolation",
        "Trust domain isolation rejects blocks from a foreign tenant before scoring begins.",
        "The trust domain is part of the policy gate; isolation means cross-tenant blocks never enter.",
        "Domain notes moved to the policy guide; the isolation section links the tenant annex.",
        "Isolation of tenant domains is owned by the security rotation and audited per packet.",
    ),
    Topic(
        "dense-vector",
        "how do we find semantically similar notes without keyword overlap",
        "Dense vector similarity ranks documents by meaning rather than surface wording.",
        "Neural passage retrieval matches paraphrases that share no literal terms.",
        "Dense notes moved to the index guide; the vector section links the similarity annex.",
        "Keyword overlap tricks rank literal matches first and miss paraphrases entirely.",
    ),
    Topic(
        "audit-ledger",
        "audit ledger replay",
        "The audit ledger replay re-runs the compiled packet from the signed epoch manifest.",
        "Ledger replay is deterministic given the epoch; the audit trail pins every input hash.",
        "Audit notes moved to the epoch guide; the replay section links the manifest annex.",
        "Ledger replay hashes are owned by the compliance rotation and archived per release.",
    ),
    Topic(
        "single-writer",
        "who may write to a shared block at the same time",
        "A single writer holds exclusive rights to each register until it releases them.",
        "Concurrent mutations are serialized behind a lease; duplicate writers wait their turn.",
        "Shared notes moved to the lease guide; the block section links the version annex.",
        "Writer blocks are owned by the storage rotation and versioned per epoch.",
    ),
    Topic(
        "canary-rollout",
        "canary rollout migration",
        "The canary rollout migration ramps traffic in steps; rollout halts on verification failure.",
        "Canary migration moves one tenant at a time; rollout state is recorded per epoch.",
        "Rollout notes moved to the release guide; the migration section links the ramp annex.",
        "Migration canary steps are owned by the release rotation and gated on probe results.",
    ),
    Topic(
        "sqlite-journal",
        "sqlite journal checkpoint",
        "Sqlite journal checkpoints flush the wal to disk; a checkpoint stall blocks writers.",
        "The journal checkpoint runs every 512 pages; wal checkpoints keep read latency flat.",
        "Journal notes moved to the storage guide; the checkpoint section links the wal annex.",
        "Checkpoint stalls are owned by the storage rotation and journaled per transaction.",
    ),
    Topic(
        "conflict-alert",
        "what happens when two facts disagree about current state",
        "Mismatched assertions raise a conflict alert; the compiler pins the older fact for review.",
        "Contradiction alerts are emitted when the closer finds opposing claims on the same subject.",
        "Facts notes moved to the closer guide; the conflict section links the alert annex.",
        "State disagreements are owned by the audit rotation and escalate to the duty reviewer.",
    ),
)

PARAPHRASE_TAGS = frozenset({"dense-vector", "single-writer", "conflict-alert"})


def _mk(bid: str, content: str, seq: int) -> ContextBlock:
    return ContextBlock(
        block_id=bid,
        block_kind=BlockKind.fact,
        memory_tier=MemoryTier.ledger,
        content=content,
        content_hash="sha256:" + bid,
        token_estimate=count_tokens(content),
        source_event_seq=seq,
        creator=Creator(type=CreatorType.agent, id="bench"),
        fidelity=Fidelity.extract,
        trust_domain="bench",
    )


def build_corpus() -> tuple[list[ContextBlock], list[tuple[str, list[str]]]]:
    """Return (blocks, queries) with labels assigned by construction."""
    blocks: list[ContextBlock] = []
    queries: list[tuple[str, list[str]]] = []
    seq = 1
    for i, t in enumerate(TOPICS):
        rel_ids = [f"blk_{t.tag}_rel_a", f"blk_{t.tag}_rel_b"]
        for bid, text in (
            (rel_ids[0], t.rel_a),
            (rel_ids[1], t.rel_b),
            (f"blk_{t.tag}_dup_a", t.dup_a),
            (f"blk_{t.tag}_dup_b", t.dup_b),
            (
                f"blk_{t.tag}_neg_a",
                f"Meeting digest: flagged {t.query} for follow-up; noise level high, no findings "
                f"recorded. {FILLER[i % len(FILLER)]}",
            ),
            (
                f"blk_{t.tag}_neg_b",
                f"Standup card {i:02d} lists {t.query}; triage deferred to next cycle. "
                f"{FILLER[(i + 3) % len(FILLER)]}",
            ),
        ):
            blocks.append(_mk(bid, text, seq))
            seq += 1
        queries.append((t.query, rel_ids))
    return blocks, queries


def lexical_baseline(query: str, blocks: Sequence[ContextBlock]) -> dict[str, float]:
    """The OLD naive overlap (pre-BM25): |query_tokens & block_tokens| / |query_tokens|."""
    obj = set(_OLD_WORD.findall(query.lower()))
    out: dict[str, float] = {}
    for b in blocks:
        text = " ".join([b.content or "", *b.symbols_exported, *b.symbols_imported])
        out[b.block_id] = len(obj & set(_OLD_WORD.findall(text.lower()))) / len(obj) if obj else 0.0
    return out


def rank(blocks: Sequence[ContextBlock], scores: dict[str, float]) -> list[str]:
    return sorted((b.block_id for b in blocks), key=lambda bid: (-scores.get(bid, 0.0), bid))


def evaluate_query(
    blocks: Sequence[ContextBlock], scores: dict[str, float], relevant: Sequence[str]
) -> dict[str, Any]:
    rel = set(relevant)
    ranking = rank(blocks, scores)
    p5 = sum(1 for bid in ranking[:5] if bid in rel) / 5.0
    r10 = sum(1 for bid in ranking[:10] if bid in rel) / len(rel)
    first = next((r for r, bid in enumerate(ranking, start=1) if bid in rel), None)
    return {
        "precision@5": p5,
        "recall@10": r10,
        "reciprocal_rank": (1.0 / first) if first else 0.0,
        "top1": 1.0 if ranking and ranking[0] in rel else 0.0,
        "first_relevant_rank": first,
        "ranked_top10": ranking[:10],
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, float]:
    keys = ("precision@5", "recall@10", "reciprocal_rank", "top1")
    return {k: sum(row[k] for row in rows) / len(rows) for k in keys}


def main() -> None:
    blocks, queries = build_corpus()
    modes: dict[str, Callable[[str, Sequence[ContextBlock]], dict[str, float] | None]] = {
        "lexical": lexical_baseline,
        "bm25": bm25_scores,
    }
    if embeddings.available():
        modes["embeddings"] = embeddings.embedding_scores
        modes["hybrid"] = embeddings.hybrid_scores

    per_query: dict[str, list[dict[str, Any]]] = {}
    aggregates: dict[str, dict[str, float]] = {}
    for mode, scorer in modes.items():
        rows: list[dict[str, Any]] = []
        for query, relevant in queries:
            scores = scorer(query, blocks)
            assert scores is not None, f"{mode} unexpectedly unavailable"
            row = evaluate_query(blocks, scores, relevant)
            row["query"] = query
            row["relevant"] = list(relevant)
            rows.append(row)
        per_query[mode] = rows
        aggregates[mode] = aggregate(rows)

    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    report: dict[str, Any] = {
        "benchmark": "recall_bench",
        "generated_at_utc": ts,
        "deterministic": True,
        "corpus": {
            "blocks": len(blocks),
            "queries": len(queries),
            "topics": len(TOPICS),
            "relevant_labels": sum(len(rel) for _, rel in queries),
            "paraphrase_queries": len(PARAPHRASE_TAGS),
        },
        "modes": list(modes),
        "embedding_model": embeddings.DEFAULT_MODEL if "embeddings" in modes else None,
        "aggregates": aggregates,
        "per_query": per_query,
    }
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"recall_bench_{ts}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"corpus: {len(blocks)} blocks, {len(queries)} queries, report -> {out}")
    header = f"{'mode':<12} {'P@5':>7} {'R@10':>7} {'MRR':>7} {'top1':>7}"
    print(header)
    print("-" * len(header))
    for mode, agg in aggregates.items():
        print(
            f"{mode:<12} {agg['precision@5']:>7.3f} {agg['recall@10']:>7.3f} "
            f"{agg['reciprocal_rank']:>7.3f} {agg['top1']:>7.3f}"
        )


if __name__ == "__main__":
    main()
