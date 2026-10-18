"""Offline packing benchmark — utility-greedy vs lambda-MMR at one budget (no GPU, no RNG).

Deterministic labeled fixture (~60 blocks): six "must-keep" answer-key decisions, four
clusters of 3-4 near-duplicate decisions in different wording, eight unique decisions, and
thirty templated distractors (facts/observations). Both arms call the real ``pack()`` at
the same budget; the only difference is ``mmr_lambda`` (1.0 = plain utility-greedy, 0.7 =
the shipped default).

Metrics per arm, over the selected blocks (tier1 + tier3 minus the instruction):

- ``avg_pairwise_jaccard`` — mean token-set Jaccard over every selected pair (redundancy;
  lower is better);
- ``must_keep_coverage`` — fraction of the six answer-key decisions selected;
- ``tokens`` — ``PackResult.total_tokens``.

Run: ``uv run python -m evals.packing_bench`` (writes
``evals/reports/packing_bench_<ts>.json``).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from kernel.cartridge.serializer import count_tokens
from kernel.router.packer import MMR_LAMBDA, PackResult, jaccard, pack, token_set
from kernel.router.recall import lexical_relevance
from protocol.models import (
    BlockKind,
    ContextBlock,
    Creator,
    CreatorType,
    Fidelity,
    MemoryTier,
    TokenBudget,
)

REPORTS = Path(__file__).parent / "reports"

OBJECTIVE = "provide port guidance for the gateway; keep canonical state retention notes consistent"

BUDGET_BASE = 800

# Answer keys: the must-keep blocks. Each carries ~4 objective tokens and a unique fact.
_MUST_KEEP = (
    "Gateway port: 7443 for the listener; all port answers include 7443.",
    "Keep canonical state under the atlas-live-01 cluster uuid.",
    "Keep retention notes: the ledger policy doc lists the 30-day window.",
    "For the auth guidance, provide Ed25519 tokens: TTL 120 s.",
    "Provide the API guidance: 480 requests per minute for tenants.",
    "For failover guidance, keep the region us-east-1.",
)

# Four topics, four rewrites each: one-word variants of one base decision, so mutual
# similarity is high and deliberate relevance stays ~equal within a cluster.
_NEAR_DUPLICATES = (
    (
        "For all services, address the gateway listener on port 7443.",
        "For all services, use the gateway listener on port 7443.",
        "For all services, route the gateway listener on port 7443.",
        "For all services, address the gateway endpoint on port 7443.",
    ),
    (
        "Keep the canonical state in Postgres with pgvector.",
        "Keep the canonical state inside Postgres with pgvector.",
        "Keep the canonical state in Postgres using pgvector.",
        "Keep the canonical state in Postgres locally with pgvector.",
    ),
    (
        "Keep the retention notes: the ledger compacts after 30 days.",
        "Keep the retention notes: ledger compacts after 30 days.",
        "Keep the retention notes: the ledger compacts at 30 days.",
        "Keep the retention notes: the ledger compacts after 30 full days.",
    ),
    (
        "Keep the guidance notes: the platform runbook.",
        "Keep the guidance notes in the platform runbook.",
        "Keep the guidance notes on the platform runbook.",
        "Keep the guidance notes: the platform runbook document.",
    ),
)

_UNIQUE_DECISIONS = (
    "For idempotency, keep the request keys consistent across service calls.",
    "For canaries, keep the rollback automatic on budget burn.",
    "For the freeze week, keep schema migrations paused.",
    "Provide tracing for every hop with the same correlation id.",
    "Keep the append-only audit log consistent on writes.",
    "For backups, keep the KMS key rotating and verify restores weekly.",
    "For page escalation, keep the 15 minute ownerless rule.",
    "Provide blue/green cutover for the reconciler.",
)

_DISTRACTOR_TOPICS = (
    "Observation shard {i}: watermark lag reached {a} ms during the {b} window; "
    "backlog drained on the platform board.",
    "Observation shard {i}: watermark lag touched {a} ms in the {b} window; the "
    "platform board confirmed drain.",
    "Incident note {i}: during the {b} window lag hit {a} ms before the platform "
    "board confirmed the drain.",
)


def _mk(
    bid: str,
    kind: BlockKind,
    content: str,
    seq: int,
) -> ContextBlock:
    return ContextBlock(
        block_id=bid,
        block_kind=kind,
        memory_tier=MemoryTier.ledger,
        content=content,
        content_hash="sha256:" + bid,
        token_estimate=count_tokens(content),
        source_event_seq=seq,
        creator=Creator(type=CreatorType.agent, id="bench"),
        fidelity=Fidelity.extract,
        trust_domain="bench",
    )


def fixture() -> tuple[list[ContextBlock], tuple[str, ...]]:
    blocks: list[ContextBlock] = []
    must_keep: list[str] = []
    seq = 1
    for i, content in enumerate(_MUST_KEEP):
        bid = f"answer-{i + 1}"
        blocks.append(_mk(bid, BlockKind.decision, content, seq))
        must_keep.append(bid)
        seq += 1
    for c, cluster in enumerate(_NEAR_DUPLICATES):
        for r, content in enumerate(cluster):
            blocks.append(_mk(f"dup-{c + 1}-{r + 1}", BlockKind.decision, content, seq))
            seq += 1
    for i, content in enumerate(_UNIQUE_DECISIONS):
        blocks.append(_mk(f"unique-{i + 1}", BlockKind.decision, content, seq))
        seq += 1
    for i in range(30):
        template = _DISTRACTOR_TOPICS[i % len(_DISTRACTOR_TOPICS)]
        content = template.format(i=i + 1, a=120 + i * 7, b=f"2026-{1 + i % 9:02d}")
        kind = BlockKind.fact if i % 3 == 0 else BlockKind.observation
        blocks.append(_mk(f"filler-{i + 1}", kind, content, seq))
        seq += 1
    return blocks, tuple(must_keep)


def _instruction() -> ContextBlock:
    content = f"OBJECTIVE: {OBJECTIVE}\nSTAGE: review\nROLE: reviewer"
    return _mk("blk_instruction", BlockKind.instruction, content, 0)


def _fixture_check() -> None:
    """Design contract of the labeled fixture (run-time, deterministic).

    Near-duplicates must be mutually similar (>= 0.6), decision relevance must be
    reasonably tight across arms (spread <= 0.35), and every must-keep must be
    objective-relevant (>= 0.2) — otherwise the greedy-vs-MMR comparison would be an
    accident of relevance rather than of redundancy.
    """
    blocks, _ = fixture()
    by_id = {b.block_id: b for b in blocks}
    for c in range(len(_NEAR_DUPLICATES)):
        ids = [f"dup-{c + 1}-{r + 1}" for r in range(4)]
        sims = [
            jaccard(token_set(by_id[a].content or ""), token_set(by_id[b].content or ""))
            for i, a in enumerate(ids)
            for b in ids[i + 1 :]
        ]
        assert min(sims) >= 0.6, f"cluster {c + 1} min pairwise similarity {min(sims):.3f}"
    decisions = [b for b in blocks if b.block_kind is BlockKind.decision]
    rel = {b.block_id: lexical_relevance(OBJECTIVE, b) for b in decisions}
    spread = max(rel.values()) - min(rel.values())
    assert spread <= 0.35, f"decision relevance spread {spread:.3f}"
    for i in range(1, len(_MUST_KEEP) + 1):
        assert rel[f"answer-{i}"] >= 0.2, f"answer-{i} relevance {rel[f'answer-{i}']:.3f}"


def _selected(packed: PackResult) -> list[ContextBlock]:
    return [
        *packed.tier1,
        *(b for b in packed.tier3 if b.block_kind is not BlockKind.instruction),
    ]


def measure(mmr_lambda: float) -> dict[str, Any]:
    blocks, must_keep = fixture()
    budget = TokenBudget.from_base(BUDGET_BASE)
    packed = pack(
        objective=OBJECTIVE,
        stage="review",
        instruction=_instruction(),
        blocks=blocks,
        budget=budget,
        cartridge_tokens=0,
        mmr_lambda=mmr_lambda,
    )
    selected = _selected(packed)
    pairs = [
        jaccard(token_set(a.content or ""), token_set(b.content or ""))
        for i, a in enumerate(selected)
        for b in selected[i + 1 :]
    ]
    selected_ids = {b.block_id for b in selected}
    return {
        "mmr_lambda": mmr_lambda,
        "selected_count": len(selected),
        "avg_pairwise_jaccard": round(sum(pairs) / len(pairs), 6) if pairs else 0.0,
        "must_keep_hit": [bid for bid in must_keep if bid in selected_ids],
        "must_keep_coverage": round(
            sum(1 for bid in must_keep if bid in selected_ids) / len(must_keep), 6
        ),
        "tokens": packed.total_tokens,
        "tier1_tokens": sum(b.token_estimate for b in packed.tier1),
        "tier3_tokens": sum(b.token_estimate for b in packed.tier3),
        "tier2_handles": len(packed.tier2),
        "selected_ids": [b.block_id for b in selected],
    }


def run() -> dict[str, Any]:
    _fixture_check()
    blocks, must_keep = fixture()
    budget = TokenBudget.from_base(BUDGET_BASE)
    arms = {
        "greedy": measure(1.0),
        "mmr": measure(MMR_LAMBDA),
    }
    return {
        "scenario": "packing_bench",
        "ts": time.strftime("%Y%m%d_%H%M%S"),
        "objective": OBJECTIVE,
        "budget": budget.model_dump(),
        "fixture": {
            "blocks": len(blocks),
            "must_keep": list(must_keep),
            "kinds": {
                kind.value: sum(1 for b in blocks if b.block_kind is kind)
                for kind in (BlockKind.decision, BlockKind.fact, BlockKind.observation)
            },
        },
        "arms": arms,
        "notes": [
            "Deterministic fixture, no RNG; both arms call kernel.router.packer.pack at "
            "the same budget and differ only in mmr_lambda.",
            "avg_pairwise_jaccard is computed over selected blocks (tier1 + tier3 minus "
            "the instruction) with the packer's token-set Jaccard.",
            "Synthetic fixture: numbers are a measurement instrument, not a product claim.",
        ],
    }


def _print(report: dict[str, Any]) -> None:
    budget = report["budget"]
    print(
        f"packing bench — {report['objective']!r}\n"
        f"fixture: {report['fixture']['blocks']} blocks "
        f"({report['fixture']['kinds']}), budget {budget['hard_total_tokens']} "
        f"(tier1 {budget['tier1']}, tier2 {budget['tier2']}, tier3 {budget['tier3']})"
    )
    print(f"{'arm':<8}{'selected':>9}{'avg_jaccard':>13}{'must_keep':>11}{'tokens':>8}")
    for name, arm in report["arms"].items():
        print(
            f"{name:<8}{arm['selected_count']:>9}{arm['avg_pairwise_jaccard']:>13.4f}"
            f"{arm['must_keep_coverage']:>11.4f}{arm['tokens']:>8}"
        )


def main() -> int:
    report = run()
    out = REPORTS / f"packing_bench_{report['ts']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    _print(report)
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
