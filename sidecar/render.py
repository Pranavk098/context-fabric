"""Compact plain-text rendering of a compiled ContextPacket for prompt injection.

Explicit render order (attention U-curve: critical constraints sit at the start AND the
end of the prompt):

1. instruction (head)
2. tier-0 cartridge hologram
3. tier-1 blocks in packed order (ascending ``source_event_seq`` — the CPP
   ``Tier1Ledger`` invariant; the packer selects by lambda-MMR but the ledger order is
   schema-fixed and prefix-stable)
4. tier-3 shadow probes
5. tier-3 observations (the packer's MMR/utility order)
6. tier-2 swap handles
7. tail: ``PROHIBITIONS`` distilled from negative knowledge (built in
   ``kernel/router/packer.py``), then the output contract, then the ``KEY CONSTRAINTS``
   recap — up to two of the most recent tier-1 decision/negative-knowledge blocks, each
   truncated to ~64 whitespace tokens (with an ellipsis), skipping blocks whose full text
   (or first 12 words, the prohibition distillate) is already rendered under
   ``PROHIBITIONS``. The recap is on by default; ``CF_RECAP=0`` disables it.
"""

from __future__ import annotations

import os

from protocol.models import BlockKind, ContextBlock, ContextPacket

_RECAP_KINDS = frozenset({BlockKind.decision, BlockKind.negative_knowledge})
_RECAP_MAX_BLOCKS = 2
_RECAP_MAX_WORDS = 64
_RECAP_HEADER = "KEY CONSTRAINTS (restated):"


def _line(block: ContextBlock) -> str:
    return f"[{block.block_kind.value}] {block.content or block.ref_uri or block.block_id}"


def _recap_lines(packet: ContextPacket) -> list[str]:
    """Up to two recency-tail constraint lines; [] when disabled or nothing eligible."""
    if os.environ.get("CF_RECAP", "1") == "0":
        return []
    anchor = packet.tiers.tier3.recency_anchor
    prohibited = frozenset(anchor.prohibitions) if anchor is not None else frozenset()
    lines: list[str] = []
    for block in reversed(packet.tiers.tier1.blocks):  # most recent first
        if block.block_kind not in _RECAP_KINDS:
            continue
        words = (block.content or "").split()
        if not words:
            continue  # empty content
        if " ".join(words) in prohibited or " ".join(words[:12]) in prohibited:
            continue  # already rendered as a prohibition line (packer keeps the first 12 words)
        text = " ".join(words[:_RECAP_MAX_WORDS])
        if len(words) > _RECAP_MAX_WORDS:
            text += "\u2026"
        lines.append(f"- [{block.block_kind.value}] {text}")
        if len(lines) == _RECAP_MAX_BLOCKS:
            break
    return lines


def render_packet(packet: ContextPacket) -> str:
    """Instruction first, constraints (prohibitions, contract, recap) last; no JSON blobs."""
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
    recap = _recap_lines(packet)
    if recap:
        parts.append(_RECAP_HEADER)
        parts.extend(recap)
    return "\n".join(parts)
