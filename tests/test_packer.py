"""Packer selection tests — lambda-MMR diversity, budget fit, determinism (no GPU)."""

from __future__ import annotations

from kernel.router.packer import MMR_LAMBDA, jaccard, pack, token_set
from protocol.models import (
    BlockKind,
    ContextBlock,
    Creator,
    CreatorType,
    Fidelity,
    MemoryTier,
    TokenBudget,
)

_OBJECTIVE = "canonical storage embeddings"


def mk(
    bid: str,
    content: str,
    *,
    seq: int = 1,
    kind: BlockKind = BlockKind.decision,
    tokens: int = 40,
) -> ContextBlock:
    return ContextBlock(
        block_id=bid,
        block_kind=kind,
        memory_tier=MemoryTier.ledger,
        content=content,
        content_hash="sha256:" + bid,
        token_estimate=tokens,
        source_event_seq=seq,
        creator=Creator(type=CreatorType.agent, id="tester"),
        fidelity=Fidelity.extract,
        trust_domain="t",
    )


def _instruction() -> ContextBlock:
    return mk(
        "blk_instruction",
        f"OBJECTIVE: {_OBJECTIVE}\nSTAGE: implement\nROLE: implementer",
        kind=BlockKind.instruction,
        tokens=10,
    )


# tier1 fits exactly two of the three 40-token decisions; tier3 fits the instruction.
_BUDGET = TokenBudget(hard_total_tokens=200, tier0=20, tier1=85, tier2=20, tier3=65, reserve=10)


def _redundant_fixture() -> list[ContextBlock]:
    return [
        mk("d1", "Use Postgres with pgvector for canonical storage embeddings", seq=1),
        mk("d2", "Use Postgres with pgvector for the canonical storage embeddings", seq=2),
        mk("d3", "Store canonical storage embeddings in the ledger", seq=3),
    ]


def test_near_duplicate_pair_with_tight_budget_keeps_only_one() -> None:
    blocks = _redundant_fixture()
    greedy = pack(
        objective=_OBJECTIVE,
        stage="implement",
        instruction=_instruction(),
        blocks=blocks,
        budget=_BUDGET,
        cartridge_tokens=0,
        mmr_lambda=1.0,
    )
    mmr = pack(
        objective=_OBJECTIVE,
        stage="implement",
        instruction=_instruction(),
        blocks=blocks,
        budget=_BUDGET,
        cartridge_tokens=0,
    )
    greedy_ids = [b.block_id for b in greedy.tier1]
    mmr_ids = [b.block_id for b in mmr.tier1]
    assert greedy_ids == ["d1", "d2"]  # utility ties broken by block_id; both near-dups fit
    assert mmr_ids == ["d1", "d3"]  # d2's similarity penalty yields to the diverse d3
    assert len(mmr_ids) == 2


def test_mmr_lowers_redundancy_at_equal_utility() -> None:
    blocks = _redundant_fixture()
    greedy = pack(
        objective=_OBJECTIVE,
        stage="implement",
        instruction=_instruction(),
        blocks=blocks,
        budget=_BUDGET,
        cartridge_tokens=0,
        mmr_lambda=1.0,
    )
    mmr = pack(
        objective=_OBJECTIVE,
        stage="implement",
        instruction=_instruction(),
        blocks=blocks,
        budget=_BUDGET,
        cartridge_tokens=0,
    )

    def avg_pairwise_jaccard(selected: tuple[ContextBlock, ...]) -> float:
        pairs = [
            jaccard(token_set(a.content or ""), token_set(b.content or ""))
            for i, a in enumerate(selected)
            for b in selected[i + 1 :]
        ]
        return sum(pairs) / len(pairs)

    assert avg_pairwise_jaccard(mmr.tier1) < avg_pairwise_jaccard(greedy.tier1)
    assert sum(b.token_estimate for b in mmr.tier1) <= _BUDGET.tier1
    assert sum(b.token_estimate for b in greedy.tier1) <= _BUDGET.tier1


def test_empty_candidates_pack_cleanly() -> None:
    instruction = _instruction()
    result = pack(
        objective=_OBJECTIVE,
        stage="implement",
        instruction=instruction,
        blocks=[],
        budget=_BUDGET,
        cartridge_tokens=0,
    )
    assert result.tier1 == ()
    assert result.tier2 == ()
    assert result.tier3 == (instruction,)
    assert result.total_tokens == 10


def test_selection_is_deterministic() -> None:
    blocks = _redundant_fixture()
    instruction = _instruction()
    first = pack(
        objective=_OBJECTIVE,
        stage="implement",
        instruction=instruction,
        blocks=blocks,
        budget=_BUDGET,
        cartridge_tokens=0,
    )
    second = pack(
        objective=_OBJECTIVE,
        stage="implement",
        instruction=instruction,
        blocks=blocks,
        budget=_BUDGET,
        cartridge_tokens=0,
    )
    assert [b.block_id for b in first.tier1] == [b.block_id for b in second.tier1]
    assert [b.block_id for b in first.tier3] == [b.block_id for b in second.tier3]
    assert first == second


def test_tier1_output_stays_event_seq_ordered() -> None:
    blocks = [b for b in reversed(_redundant_fixture())]
    result = pack(
        objective=_OBJECTIVE,
        stage="implement",
        instruction=_instruction(),
        blocks=blocks,
        budget=_BUDGET,
        cartridge_tokens=0,
    )
    seqs = [b.source_event_seq for b in result.tier1]
    assert seqs == sorted(seqs)


def test_mmr_lambda_one_reproduces_legacy_greedy_tier1() -> None:
    # Legacy greedy admitted tier-1 blocks in event-seq order; utility-greedy would pick
    # d2 first. Only one 40-token block fits the 50-token tier.
    budget = TokenBudget(hard_total_tokens=100, tier0=0, tier1=50, tier2=20, tier3=30, reserve=0)
    blocks = [
        mk("d1", "unrelated coffee chatter", seq=1),
        mk("d2", "canonical storage embeddings", seq=2),
    ]
    legacy = pack(
        objective=_OBJECTIVE,
        stage="implement",
        instruction=_instruction(),
        blocks=blocks,
        budget=budget,
        cartridge_tokens=0,
        mmr_lambda=1.0,
    )
    mmr = pack(
        objective=_OBJECTIVE,
        stage="implement",
        instruction=_instruction(),
        blocks=blocks,
        budget=budget,
        cartridge_tokens=0,
    )
    assert [b.block_id for b in legacy.tier1] == ["d1"]
    assert [b.block_id for b in mmr.tier1] == ["d2"]


def test_similarity_tokens_are_lowercased_and_min_length_two() -> None:
    assert token_set("A bb CC 7443 x") == {"bb", "cc", "7443"}
    assert jaccard(token_set(""), token_set("bb")) == 0.0
    assert jaccard(token_set("bb cc"), token_set("bb cc")) == 1.0
    assert MMR_LAMBDA == 0.7
