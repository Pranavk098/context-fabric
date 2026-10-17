"""Compact plain-text rendering of a compiled ContextPacket for prompt injection."""

from __future__ import annotations

from protocol.models import ContextBlock, ContextPacket


def _line(block: ContextBlock) -> str:
    return f"[{block.block_kind.value}] {block.content or block.ref_uri or block.block_id}"


def render_packet(packet: ContextPacket) -> str:
    """Instruction first, then decisions/facts with their content; no JSON blobs."""
    parts: list[str] = []
    instruction = packet.tiers.tier3.instruction
    if instruction is not None and instruction.content:
        parts.append(instruction.content)
    if packet.tiers.tier0.content_markdown:
        parts.append(packet.tiers.tier0.content_markdown)
    parts.extend(_line(b) for b in packet.tiers.tier1.blocks)
    parts.extend(_line(b) for b in packet.tiers.tier3.shadow_probes)
    parts.extend(_line(b) for b in packet.tiers.tier3.observations)
    parts.extend(f"[swap] {h.summary} ({h.uri})" for h in packet.tiers.tier2.handles)
    return "\n".join(parts)
