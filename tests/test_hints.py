"""Agent cache hints — deterministic RFC #52113-shaped output from a compiled packet."""

from __future__ import annotations

from kernel.router.hints import HINT_VERSION, packet_cache_hints
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
    Scope,
    Tier0Cartridge,
    Tier1Ledger,
    Tier3Register,
    Tiers,
    TokenBudget,
)


def _block(block_id: str, kind: BlockKind, content: str) -> ContextBlock:
    return ContextBlock(
        block_id=block_id,
        block_kind=kind,
        memory_tier=MemoryTier.ledger,
        content=content,
        content_hash="sha256:" + block_id,
        token_estimate=1,
        source_event_seq=1,
        creator=Creator(type=CreatorType.agent, id="tester"),
        fidelity=Fidelity.extract,
        trust_domain="t1",
    )


def _packet(
    *,
    tier0_markdown: str = "# Hologram\n- core.py: shared()",
    instruction: str = "OBJECTIVE: test\nSTAGE: implement\nROLE: implementer",
    observation: str = "observation one",
    total_tokens: int = 10,
) -> ContextPacket:
    return ContextPacket(
        packet_id="cpk_t",
        route_id="rte_t",
        watermark_event_seq=1,
        scope=Scope(tenant_id="t1", project_id="p1", workflow_id="w1", task_id="task1"),
        recipient=Recipient(agent_id="a1", role="implementer", stage="implement"),
        budget=TokenBudget(),
        tiers=Tiers(
            tier0=Tier0Cartridge(
                cartridge_id="c", kv_prefix_hash="sha256:x", content_markdown=tier0_markdown
            ),
            tier1=Tier1Ledger(blocks=(_block("d1", BlockKind.decision, "Use Postgres"),)),
            tier3=Tier3Register(
                instruction=_block("blk_i", BlockKind.instruction, instruction),
                observations=(_block("o1", BlockKind.observation, observation),),
            ),
        ),
        total_tokens=total_tokens,
        hashes=Hashes(content_hash="sha256:y"),
    )


def test_hints_are_deterministic() -> None:
    packet = _packet()
    assert packet_cache_hints(packet) == packet_cache_hints(packet)


def test_prefix_hash_changes_with_instruction_and_tier0() -> None:
    base = packet_cache_hints(_packet())
    other_instruction = packet_cache_hints(_packet(instruction="OBJECTIVE: different"))
    other_tier0 = packet_cache_hints(_packet(tier0_markdown="# Other hologram"))
    assert base["prefix_hash"] != other_instruction["prefix_hash"]
    assert base["prefix_hash"] != other_tier0["prefix_hash"]


def test_prefix_hash_ignores_tier3_observations() -> None:
    base = packet_cache_hints(_packet())
    changed = packet_cache_hints(_packet(observation="a completely different observation"))
    assert base["prefix_hash"] == changed["prefix_hash"]


def test_field_types_and_version() -> None:
    hints = packet_cache_hints(_packet())
    assert set(hints) == {
        "session_id",
        "prefix_hash",
        "ttl_seconds",
        "evict_priority",
        "hint_version",
    }
    assert hints["session_id"] == "w1:task1"
    assert isinstance(hints["prefix_hash"], str)
    assert hints["prefix_hash"].startswith("sha256:")
    assert isinstance(hints["ttl_seconds"], int) and not isinstance(hints["ttl_seconds"], bool)
    assert hints["ttl_seconds"] == 3600
    assert hints["evict_priority"] in {"low", "normal", "high"}
    assert hints["hint_version"] == HINT_VERSION == "0.1"
    assert packet_cache_hints(_packet(), ttl_seconds=60)["ttl_seconds"] == 60


def test_evict_priority_follows_budget_usage() -> None:
    assert packet_cache_hints(_packet(total_tokens=999))["evict_priority"] == "low"
    assert packet_cache_hints(_packet(total_tokens=1000))["evict_priority"] == "normal"
    assert packet_cache_hints(_packet(total_tokens=2999))["evict_priority"] == "normal"
    assert packet_cache_hints(_packet(total_tokens=3000))["evict_priority"] == "high"
    assert packet_cache_hints(_packet(total_tokens=4000))["evict_priority"] == "high"
