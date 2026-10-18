"""Packet-level COW_FORK — copy-on-write sub-agent packet spawning.

Spec: ``docs/design/Build-Plan-Validation-Gate.md:44`` — "``COW_FORK`` (sub-agent spawning
with constant window size)"; invariant 7 of ``docs/design/Arch-Spec.md`` — "Child agents
MUST share immutable Tier 0 and Tier 1 pages with parent agents and discard their ephemeral
Tier 3 scratchpad"; ``docs/design/Module-Implementation.md`` §D.2 — "Initialize Child
Packet C with the exact same tier0_cartridge_ref and tier1_ledger_blocks ... Allocate a
clean, empty Tier 3 working_register ... populated only with Child C's specific sub-task
objective, output contract, and Shadow Cortex probes".

Interpretations (the spec is silent, documented in ``docs/engineering/MMU-FOLD-FORK.md``):
* The child mounts the parent's Tier 0, Tier 1 AND Tier 2 objects by reference. Tier 0 is
  the shared rendered head, so that prefix renders byte-identically across agents; the
  identical Tier 1/2 sets stay expandable through their ``ctx://`` handles but render
  after the per-agent instruction. Only Tier 3 is rebuilt from scratch (fresh instruction
  for the child's objective/stage/role; no inherited probes, observations or anchor).
* Tier 1 is re-filtered through ``apply_policy`` for the child's role and the fork's
  ``as_of`` (CD-07, ``Module-Implementation.md`` §D.2): blocks the child may not see are
  dropped and ``total_tokens`` shrinks by their estimates. When nothing is denied the
  parent's ledger object itself is shared, so the strict COW identity holds in the common
  case.
* "Constant window size" means the child's ``total_tokens`` never grows past the effective
  budget: it is the parent's total minus the discarded Tier 3 (and any policy-denied Tier 1
  blocks) plus the fresh instruction, asserted against ``hard_total_tokens``.
  ``budget_base`` is the explicit escape hatch for a deliberately smaller/larger child
  window.
* ``spawn_depth`` increments; ``watermark_event_seq``, ``scope``, ``conflict_alerts`` and
  the epoch counter's +1 follow ADR-0005 lineage fields. Ids reuse the compiler's private
  ``_det_id``/``_instruction`` helpers so a fork is byte-identical to what ``compile_packet``
  would synthesize for the child.
"""

from __future__ import annotations

from datetime import datetime

from kernel.router.compiler import _det_id, _instruction
from kernel.router.policy_engine import apply_policy
from protocol.canonical import packet_content_hash
from protocol.models import (
    ContextPacket,
    Hashes,
    Recipient,
    Tier1Ledger,
    Tier3Register,
    Tiers,
    TokenBudget,
)
from protocol.signing import Signer


def _tier3_tokens(packet: ContextPacket) -> int:
    register = packet.tiers.tier3
    return (
        (register.instruction.token_estimate if register.instruction else 0)
        + sum(b.token_estimate for b in register.shadow_probes)
        + sum(b.token_estimate for b in register.observations)
    )


def fork_packet(
    parent: ContextPacket,
    *,
    recipient: Recipient,
    objective: str,
    stage: str,
    as_of: datetime,
    budget_base: int | None = None,
    signer: Signer | None = None,
) -> ContextPacket:
    """Spawn a COW child packet: shared Tiers 0/1/2, a fresh Tier 3, recomputed hash/id.

    The parent is never mutated (all models are frozen); two forks with identical
    arguments produce an identical child. Re-signs the content hash only when ``signer``
    is passed.
    """
    budget = parent.budget if budget_base is None else TokenBudget.from_base(budget_base)
    epoch_number = parent.epoch_number + 1
    instruction = _instruction(parent.scope, recipient, objective, stage, as_of)
    tier1_blocks, _denials = apply_policy(
        parent.tiers.tier1.blocks, role=recipient.role, as_of=as_of
    )
    if len(tier1_blocks) == len(parent.tiers.tier1.blocks):
        tier1 = parent.tiers.tier1  # nothing denied: keep the COW object shared
    else:
        tier1 = Tier1Ledger(blocks=tuple(tier1_blocks))
    denied_tokens = sum(b.token_estimate for b in parent.tiers.tier1.blocks) - sum(
        b.token_estimate for b in tier1_blocks
    )
    total_tokens = (
        parent.total_tokens - _tier3_tokens(parent) - denied_tokens + instruction.token_estimate
    )
    assert total_tokens <= budget.hard_total_tokens, (
        f"COW_FORK must keep the window constant: {total_tokens} > {budget.hard_total_tokens}"
    )

    packet = ContextPacket(
        cpp_version=parent.cpp_version,
        packet_id=_det_id(
            "cpk",
            parent.scope.workflow_id,
            parent.scope.task_id,
            recipient.agent_id,
            objective,
            str(parent.watermark_event_seq),
            parent.packet_id,
            str(epoch_number),
        ),
        parent_packet_id=parent.packet_id,
        route_id=_det_id(
            "rte",
            parent.scope.workflow_id,
            parent.scope.task_id,
            recipient.agent_id,
            objective,
            parent.packet_id,
        ),
        spawn_depth=parent.spawn_depth + 1,
        epoch_number=epoch_number,
        watermark_event_seq=parent.watermark_event_seq,
        scope=parent.scope,
        recipient=recipient,
        budget=budget,
        tiers=Tiers(
            tier0=parent.tiers.tier0,
            tier1=tier1,
            tier2=parent.tiers.tier2,
            tier3=Tier3Register(instruction=instruction),
        ),
        conflict_alerts=parent.conflict_alerts,
        total_tokens=total_tokens,
        hashes=Hashes(content_hash=""),
    )

    content_hash = packet_content_hash(packet)
    signature = signer.sign(content_hash.encode("utf-8")) if signer else None
    return packet.model_copy(
        update={
            "hashes": Hashes(
                content_hash=content_hash,
                signature=signature,
                key_id=signer.key_id if signer else None,
            )
        }
    )


if __name__ == "__main__":
    from datetime import UTC

    from kernel.router.compiler import compile_packet
    from protocol.canonical import sha256_hex
    from protocol.models import (
        BlockKind,
        ContextBlock,
        Creator,
        CreatorType,
        Fidelity,
        MemoryTier,
        Scope,
    )

    def obs(bid: str, seq: int, content: str) -> ContextBlock:
        return ContextBlock(
            block_id=bid,
            block_kind=BlockKind.observation,
            memory_tier=MemoryTier.register,
            content=content,
            content_hash="sha256:" + sha256_hex(content.encode("utf-8")),
            token_estimate=8,
            source_event_seq=seq,
            creator=Creator(type=CreatorType.tool, id="bash"),
            fidelity=Fidelity.raw,
            trust_domain="t1",
        )

    scope = Scope(tenant_id="t1", project_id="p1", workflow_id="w1", task_id="task1")
    as_of = datetime(2026, 1, 1, tzinfo=UTC)
    decision = obs("dec1", 2, "use sqlite for storage").model_copy(
        update={
            "block_kind": BlockKind.decision,
            "memory_tier": MemoryTier.ledger,
            "denied_roles": ("auditor",),
        }
    )
    parent = compile_packet(
        scope=scope,
        recipient=Recipient(agent_id="a1", role="implementer", stage="implement"),
        objective="implement storage",
        stage="implement",
        blocks=[obs("obs1", 1, "storage probe output"), decision],
        as_of=as_of,
    ).packet
    child = fork_packet(
        parent,
        recipient=Recipient(agent_id="a2", role="auditor", stage="audit"),
        objective="audit storage",
        stage="audit",
        as_of=as_of,
    )
    assert child.parent_packet_id == parent.packet_id
    assert child.epoch_number == parent.epoch_number + 1
    assert child.tiers.tier1.blocks == (), "role-denied ledger block must not reach the child"
    assert child.tiers.tier3.instruction is not None
    assert child.tiers.tier3.observations == ()
    assert child.total_tokens <= parent.budget.hard_total_tokens
    peer = fork_packet(
        parent,
        recipient=Recipient(agent_id="a3", role="implementer", stage="implement"),
        objective="continue storage",
        stage="implement",
        as_of=as_of,
    )
    assert peer.tiers.tier1 is parent.tiers.tier1, "no denials: ledger stays shared"
    again = fork_packet(
        parent,
        recipient=Recipient(agent_id="a2", role="auditor", stage="audit"),
        objective="audit storage",
        stage="audit",
        as_of=as_of,
    )
    assert again.packet_id == child.packet_id
    assert again.hashes.content_hash == child.hashes.content_hash
    print("cow_forker.py self-check: OK (RBAC-filtered ledger, fresh register, deterministic)")
