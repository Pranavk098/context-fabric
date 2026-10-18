"""Compact plain-text rendering of a compiled ContextPacket for prompt injection."""

from __future__ import annotations

from protocol.models import ContextBlock, ContextPacket


def _line(block: ContextBlock) -> str:
    return f"[{block.block_kind.value}] {block.content or block.ref_uri or block.block_id}"


def render_packet(packet: ContextPacket) -> str:
    """Instruction first, then decisions/facts with their content; no JSON blobs.

    The recency anchor is appended last (last position = highest recency, per the U-curve
    intent): the prohibitions distilled from negative-knowledge blocks, then the output
    contract. Prohibitions are the first 12 words of each negative-knowledge block
    (built in ``kernel/router/packer.py``).
    """
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
    anchor = packet.tiers.tier3.recency_anchor
    if anchor is not None and anchor.prohibitions:
        parts.append("PROHIBITIONS (approaches that failed \u2014 do not repeat):")
        parts.extend(f"- {prohibition}" for prohibition in anchor.prohibitions)
    if anchor is not None and anchor.output_contract:
        parts.append(anchor.output_contract)
    return "\n".join(parts)
