"""Deterministic-core tests (Gate-first: these are the Stage B 'check 1')."""

from __future__ import annotations

import pytest

from evals.spike_deterministic import block, build_packet
from protocol.canonical import canonicalize, packet_content_hash
from protocol.models import BlockKind, MemoryTier, TokenBudget


def test_jcs_canonical_vectors() -> None:
    assert canonicalize({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    assert canonicalize(1.0) == b"1"
    assert canonicalize(-0.0) == b"0"
    assert canonicalize(0.5) == b"0.5"


def test_packet_hash_is_deterministic() -> None:
    b1 = block("blk_1", BlockKind.decision, "Use Postgres", 1, MemoryTier.ledger)
    b2 = block("blk_2", BlockKind.fact, "Router deterministic", 2, MemoryTier.ledger)
    p1 = build_packet(2, (b1, b2))
    p2 = build_packet(2, (b1, b2))
    assert p1.hashes.content_hash == p2.hashes.content_hash


def test_signature_excluded_from_hash() -> None:
    b1 = block("blk_1", BlockKind.decision, "x", 1, MemoryTier.ledger)
    p = build_packet(1, (b1,))
    assert packet_content_hash(p) == p.hashes.content_hash


def test_tier1_is_append_only_prefix() -> None:
    b1 = block("blk_1", BlockKind.decision, "a", 1, MemoryTier.ledger)
    b2 = block("blk_2", BlockKind.decision, "b", 2, MemoryTier.ledger)
    b3 = block("blk_3", BlockKind.decision, "c", 3, MemoryTier.ledger)
    early = build_packet(2, (b1, b2))
    late = build_packet(3, (b1, b2, b3))
    early_ids = [b.block_id for b in early.tiers.tier1.blocks]
    late_ids = [b.block_id for b in late.tiers.tier1.blocks]
    assert late_ids[: len(early_ids)] == early_ids


def test_budget_proportions() -> None:
    bg = TokenBudget.from_base(4000)
    assert (bg.tier0, bg.tier1, bg.tier2, bg.tier3, bg.reserve) == (600, 1000, 200, 1800, 400)
    with pytest.raises(ValueError):
        TokenBudget.from_base(3999)
