"""Deterministic cache-aware utility scorer (invariant 9: deterministic core).

U(i) = w_r·Relevance + w_k·KindFit − w_t·EffectiveTokenCost
Scores are rounded to 6 dp; ties are broken by block_id in the packer (total order).
"""

from __future__ import annotations

from kernel.router.recall import lexical_relevance
from protocol.models import BlockKind, ContextBlock

KIND_WEIGHT: dict[BlockKind, float] = {
    BlockKind.instruction: 0.85,
    BlockKind.decision: 1.0,
    BlockKind.negative_knowledge: 1.0,
    BlockKind.shadow_probe: 0.9,
    BlockKind.fact: 0.6,
    BlockKind.artifact: 0.5,
    BlockKind.observation: 0.4,
    BlockKind.summary: 0.4,
    BlockKind.pointer: 0.3,
    BlockKind.cartridge_hologram: 0.2,
}


def utility(
    objective: str,
    block: ContextBlock,
    *,
    tier_budget: int,
    discount: float = 0.1,
    cached: bool = False,
) -> float:
    relevance = lexical_relevance(objective, block)
    kind = KIND_WEIGHT.get(block.block_kind, 0.3)
    eff_cost = (block.token_estimate / max(tier_budget, 1)) * (1.0 - discount * int(cached))
    return round(2.0 * relevance + 1.0 * kind - 1.5 * eff_cost, 6)


if __name__ == "__main__":
    from protocol.models import Creator, CreatorType, Fidelity, MemoryTier

    b = ContextBlock(
        block_id="b",
        block_kind=BlockKind.decision,
        memory_tier=MemoryTier.ledger,
        content="Use Postgres with pgvector for recall",
        content_hash="sha256:x",
        token_estimate=10,
        source_event_seq=1,
        creator=Creator(type=CreatorType.agent, id="a"),
        fidelity=Fidelity.extract,
        trust_domain="t",
    )
    hi = utility("Postgres pgvector recall", b, tier_budget=1000)
    lo = utility("unrelated", b, tier_budget=1000)
    assert hi > lo
    assert utility("Postgres pgvector recall", b, tier_budget=1000) == hi  # deterministic
    print("scorer.py self-check: OK")
