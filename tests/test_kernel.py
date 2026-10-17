"""Stage C kernel tests — cartridge, compiler determinism, policy, closure, signing."""

from __future__ import annotations

from datetime import UTC, datetime

from kernel.cartridge.ast_extractor import extract_source
from kernel.cartridge.serializer import build_cartridge
from kernel.router.compiler import compile_packet
from protocol.canonical import packet_content_hash
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
)
from protocol.signing import Signer

SCOPE = Scope(tenant_id="t1", project_id="p1", workflow_id="w1", task_id="task1")
AS_OF = datetime(2026, 1, 1, tzinfo=UTC)


def mk(
    bid: str,
    kind: BlockKind,
    seq: int,
    content: str,
    *,
    token_estimate: int = 8,
    denied_roles: tuple[str, ...] = (),
    symbols: tuple[str, ...] = (),
    lifecycle: LifecycleStatus = LifecycleStatus.routable,
    trust_domain: str = "t1",
) -> ContextBlock:
    return ContextBlock(
        block_id=bid,
        block_kind=kind,
        memory_tier=MemoryTier.ledger,
        content=content,
        content_hash="sha256:" + bid,
        token_estimate=token_estimate,
        source_event_seq=seq,
        creator=Creator(type=CreatorType.agent, id="a"),
        fidelity=Fidelity.extract,
        trust_domain=trust_domain,
        denied_roles=denied_roles,
        symbols_exported=symbols,
        lifecycle_status=lifecycle,
    )


def test_cartridge_is_deterministic() -> None:
    files = [
        extract_source("core.py", "def shared():\n    ...\n"),
        extract_source("app.py", "from core import shared\n\ndef main():\n    shared()\n"),
    ]
    c1 = build_cartridge(files, tenant_id="t1", project_id="p1", commit_hash="abc12345")
    c2 = build_cartridge(
        list(reversed(files)), tenant_id="t1", project_id="p1", commit_hash="abc12345"
    )
    assert c1 == c2
    assert c1.kv_prefix_hash.startswith("sha256:")


def test_compiler_is_deterministic_and_signed() -> None:
    signer = Signer.from_seed_hex("11" * 32)
    blocks = [
        mk("d1", BlockKind.decision, 1, "Use Postgres for canonical storage"),
        mk("f1", BlockKind.fact, 2, "The router is deterministic", token_estimate=6),
        mk("nk1", BlockKind.negative_knowledge, 3, "Do not retry naive masking"),
    ]
    kw = dict(
        scope=SCOPE,
        recipient=Recipient(agent_id="a1", role="implementer", stage="implement"),
        objective="implement canonical storage",
        stage="implement",
        blocks=blocks,
        as_of=AS_OF,
        signer=signer,
    )
    r1 = compile_packet(**kw)
    r2 = compile_packet(**kw)
    assert r1.packet.hashes.content_hash == r2.packet.hashes.content_hash
    assert packet_content_hash(r1.packet) == r1.packet.hashes.content_hash
    assert r1.packet.total_tokens <= r1.packet.budget.hard_total_tokens
    sig = r1.packet.hashes.signature
    assert sig is not None
    assert signer.verify(r1.packet.hashes.content_hash.encode(), sig)
    assert r1.packet.hashes.key_id == signer.key_id


def test_policy_role_blindness_and_trace() -> None:
    blocks = [
        mk("d1", BlockKind.decision, 1, "decision"),
        mk("obs1", BlockKind.observation, 2, "implementer rationale", denied_roles=("auditor",)),
    ]
    auditor = compile_packet(
        scope=SCOPE,
        recipient=Recipient(agent_id="aud", role="auditor", stage="audit"),
        objective="audit the change",
        stage="audit",
        blocks=blocks,
        as_of=AS_OF,
    )
    ids = [b.block_id for b in auditor.packet.tiers.tier1.blocks]
    assert "obs1" not in ids
    assert any(
        e["id"] == "obs1" and e["reason"] == "role_denied" for e in auditor.trace["excluded"]
    )


def test_dependency_and_negative_knowledge_closure() -> None:
    blocks = [
        mk("d1", BlockKind.decision, 1, "decision A"),
        mk("dep1", BlockKind.fact, 2, "prerequisite fact"),
        mk("nk1", BlockKind.negative_knowledge, 3, "token budget dead end", symbols=("token",)),
    ]
    by = {b.block_id: b for b in blocks}
    r = compile_packet(
        scope=SCOPE,
        recipient=Recipient(agent_id="a1", role="implementer", stage="implement"),
        objective="implement token budget handling",
        stage="implement",
        blocks=list(by.values()),
        relations=[("d1", "dep1", "depends_on")],
        as_of=AS_OF,
    )
    included = r.trace["included"]
    assert "dep1" in included
    assert "nk1" in included  # symbol intersects objective tokens


def test_tier1_is_event_seq_ordered_and_prefix_stable() -> None:
    blocks = [
        mk("d2", BlockKind.decision, 2, "second"),
        mk("d1", BlockKind.decision, 1, "first"),
    ]
    r = compile_packet(
        scope=SCOPE,
        recipient=Recipient(agent_id="a1", role="implementer", stage="implement"),
        objective="anything",
        stage="implement",
        blocks=blocks,
        as_of=AS_OF,
    )
    seqs = [b.source_event_seq for b in r.packet.tiers.tier1.blocks]
    assert seqs == sorted(seqs)


def test_adaptive_cartridge_absent_for_small_projects() -> None:
    r = compile_packet(
        scope=SCOPE,
        recipient=Recipient(agent_id="a1", role="implementer", stage="implement"),
        objective="small task",
        stage="implement",
        blocks=[mk("f1", BlockKind.fact, 1, "tiny")],
        as_of=AS_OF,
    )
    assert r.packet.tiers.tier0.cartridge_id == "cfx_none"


def test_trust_domain_isolation() -> None:
    foreign = mk("evil", BlockKind.decision, 1, "malicious decision", trust_domain="evil-corp")
    r = compile_packet(
        scope=SCOPE,
        recipient=Recipient(agent_id="a1", role="implementer", stage="implement"),
        objective="anything",
        stage="implement",
        blocks=[foreign],
        as_of=AS_OF,
    )
    assert "evil" not in r.trace["included"]
    assert any(
        e["id"] == "evil" and e["reason"] == "trust_domain_mismatch" for e in r.trace["excluded"]
    )


def test_watermark_excludes_future_blocks() -> None:
    past = mk("old", BlockKind.decision, 1, "old decision")
    future = mk("new", BlockKind.decision, 9, "future decision")
    r = compile_packet(
        scope=SCOPE,
        recipient=Recipient(agent_id="a1", role="implementer", stage="implement"),
        objective="anything",
        stage="implement",
        blocks=[past, future],
        watermark_event_seq=1,
        as_of=AS_OF,
    )
    assert "old" in r.trace["included"]
    assert "new" not in r.trace["included"]


def test_instruction_not_duplicated_in_observations() -> None:
    r = compile_packet(
        scope=SCOPE,
        recipient=Recipient(agent_id="a1", role="implementer", stage="implement"),
        objective="do the thing",
        stage="implement",
        blocks=[mk("f1", BlockKind.fact, 1, "do the thing fact")],
        as_of=AS_OF,
    )
    obs_ids = [b.block_id for b in r.packet.tiers.tier3.observations]
    assert "blk_instruction" not in obs_ids
    assert all(b.block_kind is not BlockKind.instruction for b in r.packet.tiers.tier3.observations)


def test_oversized_cartridge_falls_back_to_adaptive() -> None:
    from kernel.cartridge.ast_extractor import extract_source
    from kernel.cartridge.serializer import build_cartridge

    files = [
        extract_source(f"m{i}.py", "\n".join(f"def f{i}_{j}():\n    ...\n" for j in range(40)))
        for i in range(60)
    ]
    cart = build_cartridge(
        files, tenant_id="t1", project_id="p1", commit_hash="abc", budget_tokens=100000
    )
    r = compile_packet(
        scope=SCOPE,
        recipient=Recipient(agent_id="a1", role="implementer", stage="implement"),
        objective="anything",
        stage="implement",
        blocks=[mk("d1", BlockKind.decision, 1, "x")],
        cartridge=cart,
        budget_base=1000,
        as_of=AS_OF,
    )
    assert r.packet.tiers.tier0.cartridge_id == "cfx_none"  # dropped, not crash
    assert r.packet.total_tokens <= 1000
