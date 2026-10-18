"""Packet-level MMU tests — EPOCH_FOLD folding and COW_FORK packet spawning."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kernel.cartridge.serializer import count_tokens
from kernel.mmu.cow_forker import fork_packet
from kernel.mmu.epoch_folder import FOLDABLE_KINDS, fold_raw_events
from kernel.router.compiler import compile_packet
from protocol.canonical import packet_content_hash, sha256_hex
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
    TokenBudget,
)
from protocol.signing import Signer

SCOPE = Scope(tenant_id="t1", project_id="p1", workflow_id="w1", task_id="task1")
AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
LONG = " ".join(f"tok{i}" for i in range(120))


def raw(
    bid: str,
    seq: int,
    content: str,
    *,
    tokens: int = 100,
    kind: BlockKind = BlockKind.observation,
    trust_domain: str = "t1",
) -> ContextBlock:
    return ContextBlock(
        block_id=bid,
        block_kind=kind,
        memory_tier=MemoryTier.register,
        content=content,
        content_hash="sha256:" + sha256_hex(content.encode("utf-8")),
        token_estimate=tokens,
        source_event_seq=seq,
        creator=Creator(type=CreatorType.tool, id="bash"),
        fidelity=Fidelity.raw,
        trust_domain=trust_domain,
        observed_at=AS_OF,
        valid_from=AS_OF,
    )


# ---------------------------------------------------------------- EPOCH_FOLD


def test_fold_mints_fact_and_handle_above_threshold() -> None:
    big, small, big2 = (
        raw("obs_a", 1, LONG, tokens=100),
        raw("obs_b", 2, "tiny", tokens=5),
        raw("obs_c", 3, LONG + " extra", tokens=100),
    )
    result = fold_raw_events([big, small, big2], k=3)
    assert (result.folded, result.kept_raw) == (2, 1)
    assert result.ledger[1] is small

    fact = result.ledger[0]
    assert fact.block_kind is BlockKind.fact
    assert fact.memory_tier is MemoryTier.ledger
    assert fact.lifecycle_status is LifecycleStatus.routable
    assert fact.fidelity is Fidelity.extract
    assert fact.creator == Creator(type=CreatorType.tool, id="bash")
    assert fact.source_event_seq == 1
    assert fact.trust_domain == "t1"
    assert fact.observed_at == AS_OF and fact.valid_from == AS_OF
    assert fact.content is not None
    assert fact.content.startswith(" ".join(LONG.split()[:40]))
    assert fact.content.endswith("(folded from obs_a)")
    assert fact.content_hash == "sha256:" + sha256_hex(fact.content.encode("utf-8"))
    assert fact.token_estimate == count_tokens(fact.content)
    assert fact.block_id != result.ledger[2].block_id

    handle = result.handles[0]
    assert handle.uri == "ctx://obs/obs_a"
    assert handle.summary == " ".join(LONG.split()[:40])
    assert handle.tokens_raw == 100
    assert handle.sha256 == "sha256:" + sha256_hex(LONG.encode("utf-8"))


def test_fold_threshold_is_strictly_above() -> None:
    at, above = raw("obs_at", 1, LONG, tokens=60), raw("obs_above", 2, LONG, tokens=61)
    result = fold_raw_events([at, above], k=2)
    assert result.ledger[0] is at
    assert result.folded == 1 and result.kept_raw == 1


def test_fold_holds_incomplete_tail_append_only() -> None:
    events = [raw(f"obs_{i}", i, LONG) for i in range(1, 5)]
    result = fold_raw_events(events, k=3)
    assert result.folded == 3 and result.kept_raw == 1
    assert result.ledger[3] is events[3], "tail must stay raw until k events accumulate"
    assert len(result.handles) == 3


def test_fold_k_one_folds_whole_sequence() -> None:
    events = [raw("obs_a", 1, LONG), raw("obs_b", 2, LONG)]
    result = fold_raw_events(events, k=1)
    assert (result.folded, result.kept_raw) == (2, 0)
    assert all(b.block_kind is BlockKind.fact for b in result.ledger)
    assert [h.uri for h in result.handles] == ["ctx://obs/obs_a", "ctx://obs/obs_b"]


def test_fold_k_zero_rejected() -> None:
    with pytest.raises(ValueError, match="k must be >= 1"):
        fold_raw_events([], k=0)


def test_fold_empty_input() -> None:
    result = fold_raw_events([])
    assert (result.ledger, result.handles, result.folded, result.kept_raw) == ((), (), 0, 0)


def test_fold_passes_through_below_threshold_and_unfoldable_kinds() -> None:
    small = raw("obs_small", 1, "tiny", tokens=5)
    decision = raw("d1", 2, "a decision", kind=BlockKind.decision, tokens=500)
    pointer = ContextBlock(
        block_id="art_p",
        block_kind=BlockKind.artifact,
        memory_tier=MemoryTier.register,
        ref_uri="ctx://object/big",
        content_hash="sha256:x",
        token_estimate=999,
        source_event_seq=3,
        creator=Creator(type=CreatorType.tool, id="bash"),
        fidelity=Fidelity.raw,
        trust_domain="t1",
    )
    result = fold_raw_events([small, decision, pointer], k=3)
    assert result.folded == 0 and result.kept_raw == 3
    assert result.ledger == (small, decision, pointer)
    assert BlockKind.observation in FOLDABLE_KINDS and BlockKind.artifact in FOLDABLE_KINDS


def test_fold_is_deterministic_and_preserves_policy_fields() -> None:
    event = raw("obs_a", 7, LONG, trust_domain="tenant-9")
    first, second = fold_raw_events([event], k=1), fold_raw_events([event], k=1)
    assert first == second
    fact = first.ledger[0]
    assert fact.classification == event.classification
    assert fact.allowed_roles == event.allowed_roles
    assert fact.denied_roles == event.denied_roles
    assert fact.trust_domain == "tenant-9"
    draft = event.model_copy(update={"lifecycle_status": LifecycleStatus.draft})
    folded_draft = fold_raw_events([draft], k=1).ledger[0]
    assert folded_draft.lifecycle_status is LifecycleStatus.draft


def test_folded_facts_round_trip_validate() -> None:
    events = [raw("obs_a", 1, LONG), raw("obs_b", 2, LONG)]
    result = fold_raw_events(events, k=2)
    for block in result.ledger:
        assert ContextBlock.model_validate(block.model_dump()) == block


def test_fold_custom_fact_tokens_bounds_fact_and_summary() -> None:
    result = fold_raw_events([raw("obs_a", 1, LONG)], k=1, fact_tokens=3)
    fact, handle = result.ledger[0], result.handles[0]
    assert fact.content == " ".join(LONG.split()[:3]) + " (folded from obs_a)"
    assert handle.summary == " ".join(LONG.split()[:3])


# ---------------------------------------------------------------- COW_FORK


def make_parent(*, budget_base: int = 4000, signer: Signer | None = None):
    blocks = [
        raw("obs1", 3, "storage probe: schema drift in ledger", tokens=8),
        ContextBlock(
            block_id="d1",
            block_kind=BlockKind.decision,
            memory_tier=MemoryTier.ledger,
            content="Use sqlite for storage",
            content_hash="sha256:d1",
            token_estimate=8,
            source_event_seq=1,
            creator=Creator(type=CreatorType.agent, id="a1"),
            fidelity=Fidelity.extract,
            trust_domain="t1",
        ),
    ]
    result = compile_packet(
        scope=SCOPE,
        recipient=Recipient(agent_id="a1", role="implementer", stage="implement"),
        objective="implement storage",
        stage="implement",
        blocks=blocks,
        budget_base=budget_base,
        as_of=AS_OF,
        signer=signer,
    )
    assert result.packet.tiers.tier3.observations, "parent needs a populated Tier 3"
    return result.packet


CHILD = Recipient(agent_id="a2", role="auditor", stage="audit")
FORK_KW = dict(recipient=CHILD, objective="audit storage", stage="audit", as_of=AS_OF)


def test_fork_shares_tier012_objects_by_identity() -> None:
    parent = make_parent()
    child = fork_packet(parent, **FORK_KW)
    assert child.tiers.tier0 is parent.tiers.tier0
    assert child.tiers.tier1 is parent.tiers.tier1
    assert child.tiers.tier2 is parent.tiers.tier2
    assert child.tiers.tier1.model_dump() == parent.tiers.tier1.model_dump()
    assert child.scope is parent.scope
    assert child.watermark_event_seq == parent.watermark_event_seq
    assert child.conflict_alerts == parent.conflict_alerts


def test_fork_filters_tier1_blocks_denied_to_child_role() -> None:
    shared = ContextBlock(
        block_id="d_shared",
        block_kind=BlockKind.decision,
        memory_tier=MemoryTier.ledger,
        content="shared decision",
        content_hash="sha256:d_shared",
        token_estimate=8,
        source_event_seq=1,
        creator=Creator(type=CreatorType.agent, id="a1"),
        fidelity=Fidelity.extract,
        trust_domain="t1",
    )
    hidden = shared.model_copy(
        update={
            "block_id": "d_hidden",
            "content": "implementer-only decision",
            "content_hash": "sha256:d_hidden",
            "source_event_seq": 2,
            "denied_roles": ("auditor",),
        }
    )
    parent = compile_packet(
        scope=SCOPE,
        recipient=Recipient(agent_id="a1", role="implementer", stage="implement"),
        objective="implement storage",
        stage="implement",
        blocks=[shared, hidden],
        as_of=AS_OF,
    ).packet
    assert [b.block_id for b in parent.tiers.tier1.blocks] == ["d_shared", "d_hidden"]

    child = fork_packet(parent, **FORK_KW)
    assert [b.block_id for b in child.tiers.tier1.blocks] == ["d_shared"]
    assert child.tiers.tier1.blocks[0] is shared
    parent_instruction = parent.tiers.tier3.instruction
    child_instruction = child.tiers.tier3.instruction
    assert parent_instruction is not None and child_instruction is not None
    assert child.total_tokens == (
        parent.total_tokens
        - parent_instruction.token_estimate
        - hidden.token_estimate
        + child_instruction.token_estimate
    )


def test_fork_tier3_is_fresh() -> None:
    parent = make_parent()
    child = fork_packet(parent, **FORK_KW)
    instruction = child.tiers.tier3.instruction
    assert instruction is not None and instruction is not parent.tiers.tier3.instruction
    assert instruction.content == "OBJECTIVE: audit storage\nSTAGE: audit\nROLE: auditor"
    assert child.tiers.tier3.shadow_probes == ()
    assert child.tiers.tier3.observations == ()
    assert child.tiers.tier3.recency_anchor is None


def test_fork_parent_is_untouched() -> None:
    parent = make_parent()
    before = parent.model_dump()
    fork_packet(parent, **FORK_KW)
    assert parent.model_dump() == before


def test_fork_lineage_fields() -> None:
    parent = make_parent()
    child = fork_packet(parent, **FORK_KW)
    assert child.parent_packet_id == parent.packet_id
    assert child.epoch_number == parent.epoch_number + 1
    assert child.spawn_depth == parent.spawn_depth + 1
    assert child.packet_id != parent.packet_id
    grandchild = fork_packet(
        child,
        recipient=Recipient(agent_id="a3", role="corrector", stage="correct"),
        objective="fix storage",
        stage="correct",
        as_of=AS_OF,
    )
    assert grandchild.parent_packet_id == child.packet_id
    assert grandchild.spawn_depth == 2


def test_fork_is_deterministic() -> None:
    parent = make_parent()
    first, second = fork_packet(parent, **FORK_KW), fork_packet(parent, **FORK_KW)
    assert first.packet_id == second.packet_id
    assert first.route_id == second.route_id
    assert first.hashes.content_hash == second.hashes.content_hash
    assert first.model_dump() == second.model_dump()


def test_fork_budget_is_respected() -> None:
    parent = make_parent()
    child = fork_packet(parent, **FORK_KW)
    parent_tier3 = parent.tiers.tier3.instruction.token_estimate + sum(
        b.token_estimate for b in parent.tiers.tier3.observations
    )
    instruction = child.tiers.tier3.instruction
    assert instruction is not None
    assert child.total_tokens == parent.total_tokens - parent_tier3 + instruction.token_estimate
    assert child.total_tokens <= parent.budget.hard_total_tokens
    assert child.budget is parent.budget


def test_fork_budget_base_override() -> None:
    parent = make_parent(budget_base=2000)
    child = fork_packet(parent, budget_base=2000, **FORK_KW)
    assert child.budget == TokenBudget.from_base(2000)
    assert child.total_tokens <= child.budget.hard_total_tokens


def test_fork_signs_only_with_signer() -> None:
    parent = make_parent(signer=Signer.from_seed_hex("22" * 32))
    unsigned = fork_packet(parent, **FORK_KW)
    assert unsigned.hashes.signature is None and unsigned.hashes.key_id is None

    signer = Signer.from_seed_hex("33" * 32)
    signed = fork_packet(parent, signer=signer, **FORK_KW)
    assert signed.hashes.key_id == signer.key_id
    signature = signed.hashes.signature
    assert signature is not None
    assert signer.verify(signed.hashes.content_hash.encode(), signature)
    assert packet_content_hash(signed) == signed.hashes.content_hash
    assert signed.hashes.content_hash == unsigned.hashes.content_hash
