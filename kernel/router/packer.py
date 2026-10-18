"""4-Tier budget allocator + packer (invariants 1, 3, 6).

Tier 0 = cartridge; Tier 1 = settled decisions + negative knowledge (ascending
``event_seq``, prefix-stable); Tier 2 = ``ctx://`` swap handles for overflow; Tier 3 =
instruction + shadow probes + utility-ranked blocks, topped by the Recency Salience Anchor.

Block selection inside a tier uses lambda-MMR (``MMR_LAMBDA = 0.7`` by default):

    score_next = lam * utility(next) - (1 - lam) * max_jaccard(next, already_selected)

Similarity is the deterministic token-set Jaccard of lowercased alphanumeric runs of
length >= 2 (``token_set``/``jaccard``); ties break on ascending ``block_id``, so the
selection is fully deterministic. ``mmr_lambda=1.0`` reproduces the legacy greedy exactly
(tier 1 keeps event-seq admission order; tier 3 keeps utility order).
Admission logic is unchanged: candidates are visited in that order and admitted while
they fit. Tier-1 output is re-sorted by ``(source_event_seq, block_id)`` afterwards so the
ledger stays prefix-stable and satisfies the ``Tier1Ledger`` schema invariant.
"""

from __future__ import annotations

import re
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

MMR_LAMBDA = 0.7

_LEDGER_KINDS = {BlockKind.decision, BlockKind.negative_knowledge}
_TIER3_KINDS = {
    BlockKind.shadow_probe,
    BlockKind.fact,
    BlockKind.artifact,
    BlockKind.observation,
    BlockKind.summary,
    BlockKind.pointer,
}

_TOKEN = re.compile(r"[a-z0-9]{2,}")


def _tok(text: str) -> int:
    return len(_ENCODER.encode(text))


def token_set(text: str) -> frozenset[str]:
    """MMR similarity tokens: lowercased alphanumeric runs of length >= 2."""
    return frozenset(_TOKEN.findall(text.lower()))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Token-set Jaccard similarity in [0, 1]; 0.0 when either side has no tokens."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _mmr_order(
    candidates: Sequence[ContextBlock],
    *,
    objective: str,
    tier_budget: int,
    lam: float = MMR_LAMBDA,
    selected: Sequence[ContextBlock] = (),
    start_tokens: int = 0,
) -> list[ContextBlock]:
    """Order ``candidates`` by lambda-MMR; deterministic, ties on ascending block_id.

    ``selected``/``start_tokens`` describe blocks already admitted to the tier (e.g. the
    instruction and probes for tier 3). Once the tier budget is saturated no further
    candidate can be admitted, so similarity penalties stop being computed and the
    remainder keeps utility order (that is the overflow/handle path).
    """
    tokens = {b.block_id: token_set(b.content or "") for b in candidates}
    utilities = {b.block_id: utility(objective, b, tier_budget=tier_budget) for b in candidates}
    best_sim = {b.block_id: 0.0 for b in candidates}
    for chosen in selected:
        chosen_tokens = token_set(chosen.content or "")
        for block_id in best_sim:
            sim = jaccard(tokens[block_id], chosen_tokens)
            if sim > best_sim[block_id]:
                best_sim[block_id] = sim

    by_id = {b.block_id: b for b in candidates}
    remaining = set(best_sim)
    min_est = min((b.token_estimate for b in candidates), default=0)
    order: list[ContextBlock] = []
    spent = start_tokens
    while remaining:
        if spent + min_est > tier_budget:
            order.extend(
                sorted(
                    (by_id[block_id] for block_id in remaining),
                    key=lambda b: (-utilities[b.block_id], b.block_id),
                )
            )
            break
        block_id = min(
            remaining,
            key=lambda bid: (
                -round(lam * utilities[bid] - (1.0 - lam) * best_sim[bid], 6),
                bid,
            ),
        )
        remaining.discard(block_id)
        order.append(by_id[block_id])
        if spent + by_id[block_id].token_estimate <= tier_budget:
            spent += by_id[block_id].token_estimate
            for other in remaining:
                sim = jaccard(tokens[other], tokens[block_id])
                if sim > best_sim[other]:
                    best_sim[other] = sim
    return order


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
    mmr_lambda: float = MMR_LAMBDA,
    output_contract: str | None = None,
) -> PackResult:
    ledger = sorted(
        (b for b in blocks if b.block_kind in _LEDGER_KINDS),
        key=lambda b: (b.source_event_seq, b.block_id),
    )
    tier1: list[ContextBlock] = []
    t1 = 0
    overflow: list[ContextBlock] = []
    tier1_order = (
        ledger
        if mmr_lambda == 1.0  # legacy admission order, not utility order
        else _mmr_order(ledger, objective=objective, tier_budget=budget.tier1, lam=mmr_lambda)
    )
    for b in tier1_order:
        if t1 + b.token_estimate <= budget.tier1:
            tier1.append(b)
            t1 += b.token_estimate
        else:
            overflow.append(b)
    tier1.sort(key=lambda b: (b.source_event_seq, b.block_id))

    probes = sorted(
        (b for b in blocks if b.block_kind is BlockKind.shadow_probe),
        key=lambda b: b.block_id,
    )
    others = [
        b
        for b in blocks
        if b.block_kind in _TIER3_KINDS and b.block_kind is not BlockKind.shadow_probe
    ]

    tier3: list[ContextBlock] = [instruction]
    t3 = instruction.token_estimate
    for b in probes:
        if t3 + b.token_estimate <= budget.tier3:
            tier3.append(b)
            t3 += b.token_estimate
        else:
            overflow.append(b)
    for b in _mmr_order(
        others,
        objective=objective,
        tier_budget=budget.tier3,
        lam=mmr_lambda,
        selected=tuple(tier3),
        start_tokens=t3,
    ):
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
        output_contract=output_contract
        or f"Stage '{stage}': reply with the required structured output only.",
        prohibitions=prohibitions,
    )

    total = cartridge_tokens + t1 + t2 + t3
    # If over budget, drop lowest-MMR tier-3 blocks (never the instruction).
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
