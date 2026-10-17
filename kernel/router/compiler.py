"""The 9-stage deterministic context compiler (CPP v1.0).

Pipeline: Interpret -> Recall -> Hard-Policy -> Score -> Dependency/Contradiction/
Negative-Knowledge Closure -> 4-Tier Allocation -> MMR/Greedy Packing -> U-Curve Anchor ->
Verify/Hash/Sign. Fully deterministic given identical inputs (ADR-0003).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from kernel.cartridge.serializer import Cartridge, count_tokens
from kernel.router.dependency_closer import Relation, close
from kernel.router.packer import pack
from kernel.router.policy_engine import Denial, apply_policy
from kernel.router.recall import lexical_relevance
from kernel.router.scorer import utility
from protocol.canonical import canonicalize, packet_content_hash, sha256_hex
from protocol.models import (
    BlockKind,
    ContextBlock,
    ContextPacket,
    Creator,
    CreatorType,
    Fidelity,
    Hashes,
    MemoryTier,
    Recipient,
    RouteTraceRef,
    Scope,
    Tier0Cartridge,
    Tier1Ledger,
    Tier2Swap,
    Tier3Register,
    Tiers,
    TokenBudget,
)
from protocol.signing import Signer

DEFAULT_AS_OF = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class CompileResult:
    packet: ContextPacket
    trace: dict[str, Any] = field(default_factory=dict)


def _det_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{sha256_hex(canonicalize(list(parts)))[:16]}"


def _instruction(
    scope: Scope, recipient: Recipient, objective: str, stage: str, as_of: datetime
) -> ContextBlock:
    content = f"OBJECTIVE: {objective}\nSTAGE: {stage}\nROLE: {recipient.role}"
    return ContextBlock(
        block_id="blk_instruction",
        block_kind=BlockKind.instruction,
        memory_tier=MemoryTier.register,
        content=content,
        content_hash="sha256:" + sha256_hex(content.encode("utf-8")),
        token_estimate=count_tokens(content),
        source_event_seq=0,
        creator=Creator(type=CreatorType.human, id="fabric"),
        fidelity=Fidelity.extract,
        trust_domain=scope.tenant_id,
        observed_at=as_of,
        valid_from=as_of,
    )


def compile_packet(
    *,
    scope: Scope,
    recipient: Recipient,
    objective: str,
    stage: str,
    blocks: Sequence[ContextBlock],
    relations: Sequence[Relation] = (),
    cartridge: Cartridge | None = None,
    watermark_event_seq: int = 0,
    budget_base: int = 4000,
    as_of: datetime = DEFAULT_AS_OF,
    signer: Signer | None = None,
) -> CompileResult:
    budget = TokenBudget.from_base(budget_base)

    # Stage 2: hard policy BEFORE scoring (deny-by-default: trust domain, RBAC, lifecycle).
    allowed, denials = apply_policy(
        blocks, role=recipient.role, as_of=as_of, trust_domain=scope.tenant_id
    )
    # Never include blocks observed after the pinned watermark.
    if watermark_event_seq > 0:
        kept: list[ContextBlock] = []
        for b in allowed:
            if b.source_event_seq > watermark_event_seq:
                denials.append(Denial(b.block_id, "after_watermark"))
            else:
                kept.append(b)
        allowed = kept
    scores = {b.block_id: utility(objective, b, tier_budget=budget.tier3) for b in allowed}

    # Stage 4: closure. Seed = objective-relevant blocks + always-kept kinds; the closer then
    # force-includes depends_on parents, contradiction counterparts, and negative knowledge.
    always = {BlockKind.decision, BlockKind.negative_knowledge, BlockKind.shadow_probe}
    seed = [b for b in allowed if b.block_kind in always or lexical_relevance(objective, b) > 0.0]
    closure = close(seed, {b.block_id: b for b in allowed}, relations, objective)

    # Stages 5/6: 4-tier allocation + packing.
    instruction = _instruction(scope, recipient, objective, stage, as_of)
    # Adaptive fallback: a Tier 0 cartridge larger than the budget (less reserve) is dropped.
    if cartridge is not None and cartridge.token_count > budget.hard_total_tokens - budget.reserve:
        cartridge = None
    if cartridge is not None:
        tier0 = Tier0Cartridge(
            cartridge_id=cartridge.cartridge_id,
            kv_prefix_hash=cartridge.kv_prefix_hash,
            content_markdown=cartridge.hologram_markdown,
        )
        cart_tokens = cartridge.token_count
    else:  # adaptive: no Tier 0 for small projects
        tier0 = Tier0Cartridge(
            cartridge_id="cfx_none", kv_prefix_hash="sha256:none", content_markdown=""
        )
        cart_tokens = 0

    packed = pack(
        objective=objective,
        stage=stage,
        instruction=instruction,
        blocks=closure.blocks,
        budget=budget,
        cartridge_tokens=cart_tokens,
    )

    probes = tuple(b for b in packed.tier3 if b.block_kind is BlockKind.shadow_probe)
    observations = tuple(
        b
        for b in packed.tier3
        if b.block_kind not in {BlockKind.shadow_probe, BlockKind.instruction}
    )

    packet = ContextPacket(
        cpp_version="1.0",
        packet_id=_det_id(
            "cpk",
            scope.workflow_id,
            scope.task_id,
            recipient.agent_id,
            objective,
            str(watermark_event_seq),
        ),
        route_id=_det_id("rte", scope.workflow_id, scope.task_id, recipient.agent_id, objective),
        spawn_depth=0,
        epoch_number=1,
        watermark_event_seq=watermark_event_seq,
        scope=scope,
        recipient=recipient,
        budget=budget,
        tiers=Tiers(
            tier0=tier0,
            tier1=Tier1Ledger(blocks=packed.tier1),
            tier2=Tier2Swap(handles=packed.tier2),
            tier3=Tier3Register(
                instruction=packed.instruction,
                shadow_probes=probes,
                observations=observations,
                recency_anchor=packed.recency_anchor,
            ),
        ),
        conflict_alerts=tuple(closure.alerts),
        total_tokens=packed.total_tokens,
        hashes=Hashes(content_hash=""),
        route_trace_ref=RouteTraceRef(),
    )

    # Stage 8: verify, hash, sign.
    content_hash = packet_content_hash(packet)
    signature = signer.sign(content_hash.encode("utf-8")) if signer else None
    packet = packet.model_copy(
        update={
            "hashes": Hashes(
                content_hash=content_hash,
                signature=signature,
                key_id=signer.key_id if signer else None,
            )
        }
    )

    included = [b.block_id for b in packed.tier1] + [b.block_id for b in packed.tier3]
    trace = {
        "included": included,
        "excluded": [{"id": d.block_id, "reason": d.reason} for d in denials],
        "scores": scores,
        "conflict_alerts": [a.model_dump() for a in closure.alerts],
        "tier_tokens": {
            "tier0": cart_tokens,
            "tier1": sum(b.token_estimate for b in packed.tier1),
            "tier2": sum(count_tokens(h.summary) + 8 for h in packed.tier2),
            "tier3": sum(b.token_estimate for b in packed.tier3),
        },
        "total_tokens": packet.total_tokens,
        "content_hash": content_hash,
    }
    return CompileResult(packet=packet, trace=trace)
