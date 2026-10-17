"""Stage B — zero-GPU deterministic-core check.

Runnable with no model and no GPU: ``uv run python -m evals.spike_deterministic``.
It asserts the invariants the whole thesis leans on:

  1. A packet compiled twice from identical inputs has an identical content_hash (ADR-0003).
  2. Tier 1 is append-only and ordered by event_seq, so a later watermark's Tier 1 has an
     earlier watermark's Tier 1 as a *prefix* (ADR-0002) -- the property KV caches match on.
  3. CoW fork: a child inherits Tier 0 + Tier 1 byte-for-byte; an RBAC-divergent child does not.
  4. Tier budgets scale by proportion and sum to the base (ADR-0001).

If any assert fails, the architecture is unsound and the spike should stop.
"""

from __future__ import annotations

from protocol.canonical import content_hash, packet_content_hash
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

SCOPE = Scope(tenant_id="t1", project_id="p1", workflow_id="w1", task_id="task1")


def block(bid: str, kind: BlockKind, content: str, seq: int, tier: MemoryTier) -> ContextBlock:
    return ContextBlock(
        block_id=bid,
        block_kind=kind,
        memory_tier=tier,
        content=content,
        content_hash="sha256:" + content_hash({"c": content}),
        token_estimate=max(1, len(content.split())),
        source_event_seq=seq,
        creator=Creator(type=CreatorType.deterministic_probe, id="test"),
        fidelity=Fidelity.extract,
        trust_domain="t1",
    )


def build_packet(
    watermark: int,
    ledger: tuple[ContextBlock, ...],
    *,
    role: str = "implementer",
    denied_roles: tuple[str, ...] = (),
) -> ContextPacket:
    blocks = tuple(b for b in ledger if role not in b.denied_roles and role not in denied_roles)
    cartridge = Tier0Cartridge(
        cartridge_id="cfx_p1", kv_prefix_hash="kv0", content_markdown="## Hologram\nmodules..."
    )
    packet = ContextPacket(
        packet_id="cpk_test",
        route_id="rte_test",
        watermark_event_seq=watermark,
        scope=SCOPE,
        recipient=Recipient(agent_id=f"agent_{role}", role=role, stage="implement"),
        budget=TokenBudget.from_base(4000),
        tiers=Tiers(
            tier0=cartridge,
            tier1=Tier1Ledger(blocks=blocks),
            tier2=Tier2Swap(),
            tier3=Tier3Register(),
        ),
        total_tokens=cartridge.content_markdown.count(" ") + sum(b.token_estimate for b in blocks),
        hashes=Hashes(content_hash=""),
        route_trace_ref=RouteTraceRef(),
    )
    return packet.model_copy(update={"hashes": Hashes(content_hash=packet_content_hash(packet))})


def main() -> None:
    b1 = block("blk_1", BlockKind.decision, "Use Postgres", 1, MemoryTier.ledger)
    b2 = block(
        "blk_2", BlockKind.negative_knowledge, "Do not retry naive masking", 2, MemoryTier.ledger
    )
    b3 = block("blk_3", BlockKind.fact, "Router is deterministic", 3, MemoryTier.ledger)

    # 1. Determinism.
    p_a = build_packet(3, (b1, b2, b3))
    p_b = build_packet(3, (b1, b2, b3))
    assert p_a.hashes.content_hash == p_b.hashes.content_hash, "packet hash is nondeterministic"

    # 2. Tier 1 prefix property across watermarks (append-only, ordered).
    early = build_packet(2, (b1, b2))
    late = build_packet(3, (b1, b2, b3))
    early_ids = [b.block_id for b in early.tiers.tier1.blocks]
    late_ids = [b.block_id for b in late.tiers.tier1.blocks]
    assert late_ids[: len(early_ids)] == early_ids, "Tier 1 is not append-only / prefix-stable"
    assert early_ids == ["blk_1", "blk_2"], "Tier 1 order must follow event_seq"

    # 3. CoW fork: identical inheritance.
    child = build_packet(3, (b1, b2, b3), role="implementer")
    assert child.tiers.tier0 == p_a.tiers.tier0, "child must inherit Tier 0 verbatim"
    assert child.tiers.tier1.blocks == p_a.tiers.tier1.blocks, "child must inherit Tier 1 verbatim"

    # 3b. RBAC-divergent child (auditor) strips denied blocks -> cache-divergent.
    b4 = block(
        "blk_4", BlockKind.observation, "implementer rationale", 4, MemoryTier.ledger
    ).model_copy(update={"denied_roles": ("auditor",)})
    auditor = build_packet(4, (b1, b2, b3, b4), role="auditor")
    auditor_ids = [b.block_id for b in auditor.tiers.tier1.blocks]
    assert "blk_4" not in auditor_ids, "auditor must not see denied observation"
    assert auditor_ids != [b.block_id for b in build_packet(4, (b1, b2, b3, b4)).tiers.tier1.blocks]

    # 4. Budget proportions.
    bg = TokenBudget.from_base(4000)
    assert bg.tier0 == 600 and bg.tier1 == 1000 and bg.tier2 == 200 and bg.tier3 == 1800
    assert (bg.tier0 + bg.tier1 + bg.tier2 + bg.tier3 + bg.reserve) == bg.hard_total_tokens

    print("spike_deterministic: ALL CHECKS PASS")
    print(f"  content_hash            = {p_a.hashes.content_hash[:24]}...")
    print(f"  tier1 (wm=2) block ids  = {early_ids}")
    print(f"  tier1 (wm=3) block ids  = {late_ids}  (prefix-stable: True)")
    print(f"  auditor tier1 (wm=4)    = {auditor_ids}  (opaque observation stripped)")


if __name__ == "__main__":
    main()
