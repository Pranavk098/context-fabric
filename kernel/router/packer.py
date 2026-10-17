"""4-Tier budget allocator + packer (invariants 1, 3, 6).

Tier 0 = cartridge; Tier 1 = settled decisions + negative knowledge (ascending
``event_seq``, prefix-stable); Tier 2 = ``ctx://`` swap handles for overflow; Tier 3 =
instruction + shadow probes + utility-ranked blocks, topped by the Recency Salience Anchor.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import tiktoken

from kernel.router.scorer import utility
from protocol.canonical import sha256_hex
from protocol.models import (
    BlockKind,
    ContextBlock,
    RecencyAnchor,
    SwapHandle,
    TokenBudget,
)

_ENCODER = tiktoken.get_encoding("o200k_base")

_LEDGER_KINDS = {BlockKind.decision, BlockKind.negative_knowledge}
_TIER3_KINDS = {
    BlockKind.shadow_probe,
    BlockKind.fact,
    BlockKind.artifact,
    BlockKind.observation,
    BlockKind.summary,
    BlockKind.pointer,
}


def _tok(text: str) -> int:
    return len(_ENCODER.encode(text))


def _handle(block: ContextBlock) -> SwapHandle:
    summary = " ".join((block.content or "").split()[:8]) or "(evicted)"
    return SwapHandle(
        uri=f"ctx://block/{block.block_id}",
        summary=summary,
        tokens_raw=block.token_estimate,
        sha256="sha256:" + sha256_hex((block.content or "").encode("utf-8")),
    )


@dataclass(frozen=True)
class PackResult:
    tier1: tuple[ContextBlock, ...]
    tier2: tuple[SwapHandle, ...]
    tier3: tuple[ContextBlock, ...]
    instruction: ContextBlock
    recency_anchor: RecencyAnchor
    cartridge_tokens: int
    total_tokens: int


def pack(
    *,
    objective: str,
    stage: str,
    instruction: ContextBlock,
    blocks: Sequence[ContextBlock],
    budget: TokenBudget,
    cartridge_tokens: int,
) -> PackResult:
    ledger = sorted(
        (b for b in blocks if b.block_kind in _LEDGER_KINDS),
        key=lambda b: (b.source_event_seq, b.block_id),
    )
    tier1: list[ContextBlock] = []
    t1 = 0
    overflow: list[ContextBlock] = []
    for b in ledger:
        if t1 + b.token_estimate <= budget.tier1:
            tier1.append(b)
            t1 += b.token_estimate
        else:
            overflow.append(b)

    probes = sorted(
        (b for b in blocks if b.block_kind is BlockKind.shadow_probe),
        key=lambda b: b.block_id,
    )
    others = [
        b
        for b in blocks
        if b.block_kind in _TIER3_KINDS and b.block_kind is not BlockKind.shadow_probe
    ]
    others.sort(key=lambda b: (-utility(objective, b, tier_budget=budget.tier3), b.block_id))

    tier3: list[ContextBlock] = [instruction]
    t3 = instruction.token_estimate
    for b in [*probes, *others]:
        if t3 + b.token_estimate <= budget.tier3:
            tier3.append(b)
            t3 += b.token_estimate
        else:
            overflow.append(b)

    handles: list[SwapHandle] = []
    t2 = 0
    for b in overflow:
        h = _handle(b)
        h_tokens = _tok(h.summary) + 8
        if t2 + h_tokens <= budget.tier2:
            handles.append(h)
            t2 += h_tokens

    prohibitions = tuple(
        (" ".join((b.content or "").split()[:12]) or b.block_id)
        for b in blocks
        if b.block_kind is BlockKind.negative_knowledge
    )
    anchor = RecencyAnchor(
        output_contract=f"Stage '{stage}': reply with the required structured output only.",
        prohibitions=prohibitions,
    )

    total = cartridge_tokens + t1 + t2 + t3
    # If over budget, drop lowest-utility tier-3 blocks (never the instruction).
    while total > budget.hard_total_tokens and len(tier3) > 1:
        removed = tier3.pop()
        total -= removed.token_estimate

    return PackResult(
        tier1=tuple(tier1),
        tier2=tuple(handles),
        tier3=tuple(tier3),
        instruction=instruction,
        recency_anchor=anchor,
        cartridge_tokens=cartridge_tokens,
        total_tokens=total,
    )
