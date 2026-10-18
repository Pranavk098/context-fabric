"""Renderer tests — the recency anchor (prohibitions + output contract) lands last."""

from __future__ import annotations

from protocol.models import (
    BlockKind,
    ContextBlock,
    ContextPacket,
    Creator,
    CreatorType,
    Fidelity,
    Hashes,
    MemoryTier,
    RecencyAnchor,
    Recipient,
    Scope,
    Tier0Cartridge,
    Tier1Ledger,
    Tier3Register,
    Tiers,
    TokenBudget,
)
from sidecar.render import render_packet

_HEADER = "PROHIBITIONS (approaches that failed \u2014 do not repeat):"


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
        trust_domain="default",
    )


def _packet(anchor: RecencyAnchor | None) -> ContextPacket:
    return ContextPacket(
        packet_id="cpk_t",
        route_id="rte_t",
        watermark_event_seq=1,
        scope=Scope(tenant_id="t", project_id="p", workflow_id="w", task_id="k"),
        recipient=Recipient(agent_id="a", role="user", stage="chat"),
        budget=TokenBudget(),
        tiers=Tiers(
            tier0=Tier0Cartridge(cartridge_id="c", kv_prefix_hash="sha256:x", content_markdown=""),
            tier1=Tier1Ledger(blocks=(_block("d1", BlockKind.decision, "Use Postgres"),)),
            tier3=Tier3Register(
                instruction=_block("blk_i", BlockKind.instruction, "OBJECTIVE: test"),
                recency_anchor=anchor,
            ),
        ),
        total_tokens=10,
        hashes=Hashes(content_hash="sha256:y"),
    )


def test_prohibitions_render_after_every_other_section_with_contract_last() -> None:
    anchor = RecencyAnchor(
        output_contract="Reply with the required structured output only.",
        prohibitions=("Dead end: do NOT scale ingestion with mirror-poller", "Second dead end"),
    )
    lines = render_packet(_packet(anchor)).splitlines()
    assert _HEADER in lines
    assert "- Dead end: do NOT scale ingestion with mirror-poller" in lines
    assert "- Second dead end" in lines
    assert lines[-1] == anchor.output_contract
    header_index = lines.index(_HEADER)
    assert header_index > max(i for i, line in enumerate(lines) if line.startswith("["))


def test_empty_prohibitions_skip_header_but_keep_contract() -> None:
    text = render_packet(_packet(RecencyAnchor(output_contract="Contract.")))
    assert _HEADER not in text
    assert text.endswith("Contract.")


def test_none_anchor_renders_without_anchor_sections() -> None:
    text = render_packet(_packet(None))
    assert text.splitlines()[0] == "OBJECTIVE: test"
    assert _HEADER not in text
    assert "required structured output" not in text
