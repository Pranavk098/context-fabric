"""raw_llm adapters — provider request fragments and cache-breakpoint placement."""

from __future__ import annotations

import pytest

from adapters.raw_llm import (
    openai_request_extras,
    openai_system_message,
    shared_prefix,
    to_anthropic,
)
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
from sidecar.render import render_packet

_TIER0 = "# Hologram\n- core.py: shared()"
_INSTRUCTION = "OBJECTIVE: test\nSTAGE: implement"


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
    tier0_markdown: str = _TIER0,
    instruction: str = _INSTRUCTION,
    observation: str = "observation one",
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
        total_tokens=10,
        hashes=Hashes(content_hash="sha256:y"),
    )


def test_openai_system_message_renders_the_packet() -> None:
    packet = _packet()
    message = openai_system_message(packet)
    assert message["role"] == "system"
    assert message["content"] == render_packet(packet)
    assert message["content"].startswith(_TIER0)


def test_openai_request_extras_is_empty_regardless_of_cartridge() -> None:
    assert openai_request_extras(_packet()) == {}
    assert openai_request_extras(_packet(tier0_markdown="")) == {}


def test_anthropic_two_blocks_pin_only_the_shared_prefix() -> None:
    packet = _packet()
    blocks = to_anthropic(packet)["system"]
    assert [block["type"] for block in blocks] == ["text", "text"]
    assert blocks[0]["text"] == _TIER0 == shared_prefix(packet)
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[1]
    assert str(blocks[1]["text"]).startswith(_INSTRUCTION)


def test_anthropic_blocks_reassemble_to_the_rendered_text() -> None:
    packet = _packet()
    blocks = to_anthropic(packet)["system"]
    joined = "\n".join(str(block["text"]) for block in blocks)
    assert joined == render_packet(packet)
    assert openai_system_message(packet)["content"] == joined  # parity between adapters


def test_anthropic_single_unpinned_block_without_cartridge() -> None:
    packet = _packet(tier0_markdown="")
    blocks = to_anthropic(packet)["system"]
    assert len(blocks) == 1
    assert "cache_control" not in blocks[0]
    assert blocks[0]["text"] == render_packet(packet)


def test_shared_prefix_is_the_renders_leading_bytes() -> None:
    packet = _packet()
    prefix = shared_prefix(packet)
    rendered = render_packet(packet)
    assert prefix == _TIER0
    assert rendered.startswith(prefix)
    assert rendered.removeprefix(prefix).startswith("\n")


def test_agents_share_block_one_and_diverge_in_block_two() -> None:
    first = to_anthropic(_packet(instruction="OBJECTIVE_ALPHA_SENTINEL: pick A"))["system"]
    second = to_anthropic(_packet(instruction="OBJECTIVE_BETA_SENTINEL: pick B"))["system"]
    assert (
        first[0]
        == second[0]
        == {"type": "text", "text": _TIER0, "cache_control": {"type": "ephemeral"}}
    )
    assert "SENTINEL" not in str(first[0]["text"])
    assert first[1] != second[1]


def test_adapters_are_deterministic() -> None:
    packet = _packet()
    assert to_anthropic(packet) == to_anthropic(packet)
    assert openai_system_message(packet) == openai_system_message(packet)
    assert openai_request_extras(packet) == openai_request_extras(packet)


def test_empty_tier0_is_instruction_first_and_unpinned() -> None:
    packet = _packet(tier0_markdown="")
    assert shared_prefix(packet) == ""
    blocks = to_anthropic(packet)["system"]
    assert len(blocks) == 1
    assert str(blocks[0]["text"]).startswith(_INSTRUCTION)
    assert openai_system_message(packet)["content"].startswith(_INSTRUCTION)


def test_cache_ttl_maps_to_documented_field_shape() -> None:
    assert to_anthropic(_packet())["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert to_anthropic(_packet(), cache_ttl="5m")["system"][0]["cache_control"] == {
        "type": "ephemeral",
        "ttl": "5m",
    }
    assert to_anthropic(_packet(), cache_ttl="1h")["system"][0]["cache_control"] == {
        "type": "ephemeral",
        "ttl": "1h",
    }
    no_cartridge = to_anthropic(_packet(tier0_markdown=""), cache_ttl="1h")["system"][0]
    assert "cache_control" not in no_cartridge  # nothing shared to pin; TTL is moot


def test_invalid_cache_ttl_rejected_before_it_reaches_the_api() -> None:
    with pytest.raises(ValueError, match="cache_ttl"):
        to_anthropic(_packet(), cache_ttl="2h")  # type: ignore[arg-type]
