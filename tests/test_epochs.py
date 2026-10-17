"""Copy-on-write memory state tests — fork sharing, isolation, fold contract, determinism."""

from __future__ import annotations

import pytest

from kernel.epochs import (
    FoldConflict,
    child_add,
    child_remove,
    cow_fork,
    epoch_fold,
    new_state,
)
from protocol.canonical import sha256_hex
from protocol.models import BlockKind, ContextBlock, Creator, CreatorType, Fidelity, MemoryTier


def mk(bid: str, seq: int, content: str) -> ContextBlock:
    return ContextBlock(
        block_id=bid,
        block_kind=BlockKind.fact,
        memory_tier=MemoryTier.ledger,
        content=content,
        content_hash="sha256:" + sha256_hex(content.encode("utf-8")),
        token_estimate=8,
        source_event_seq=seq,
        creator=Creator(type=CreatorType.agent, id="a"),
        fidelity=Fidelity.extract,
        trust_domain="t",
    )


def test_state_id_is_deterministic_and_order_independent() -> None:
    b1, b2 = mk("b1", 1, "one"), mk("b2", 2, "two")
    s1 = new_state([b1, b2])
    s2 = new_state([b2, b1])
    assert s1.state_id == s2.state_id
    c1 = child_add(cow_fork(s1), mk("b3", 3, "three"))
    c2 = child_add(cow_fork(s2), mk("b3", 3, "three"))
    assert c1.state_id == c2.state_id


def test_state_id_changes_with_content_and_lineage() -> None:
    root = new_state([mk("b1", 1, "one")])
    changed = new_state([mk("b1", 1, "one changed")])
    assert root.state_id != changed.state_id
    child = cow_fork(root)
    assert child.state_id != root.state_id
    assert cow_fork(child).state_id != child.state_id


def test_fork_shares_block_objects_without_copying() -> None:
    parent = new_state([mk("b1", 1, "one"), mk("b2", 2, "two")])
    child = cow_fork(parent)
    assert child.parent_state_id == parent.state_id
    assert child.blocks == parent.blocks
    assert all(c is p for c, p in zip(child.blocks, parent.blocks, strict=True))


def test_isolation_both_directions() -> None:
    parent = new_state([mk("b1", 1, "one")])
    parent_id = parent.state_id
    child = cow_fork(parent)
    child_id = child.state_id
    edited = child_add(child, mk("b2", 2, "two"))
    removed = child_remove(edited, "b1")
    assert [b.block_id for b in parent.blocks] == ["b1"]
    assert [b.block_id for b in removed.blocks] == ["b2"]
    merged = epoch_fold(edited, parent)
    assert merged.added == 1
    assert [b.block_id for b in parent.blocks] == ["b1"]
    assert parent.state_id == parent_id
    assert [b.block_id for b in child.blocks] == ["b1"]
    assert child.state_id == child_id


def test_child_add_identical_block_is_noop() -> None:
    child = cow_fork(new_state([mk("b1", 1, "one")]))
    assert child_add(child, mk("b1", 1, "one")) is child
    added = child_add(child, mk("b2", 2, "two"))
    assert added is not child
    assert added.parent_state_id == child.parent_state_id


def test_child_add_same_id_different_content_conflicts() -> None:
    child = cow_fork(new_state([mk("b1", 1, "one")]))
    with pytest.raises(FoldConflict) as exc:
        child_add(child, mk("b1", 1, "one rewritten"))
    assert "b1" in exc.value.detail


def test_new_state_rejects_duplicate_block_ids() -> None:
    b1 = mk("b1", 1, "one")
    with pytest.raises(ValueError, match="unique block_id"):
        new_state([b1, b1])
    with pytest.raises(ValueError, match="unique block_id"):
        new_state([b1, mk("b1", 1, "one changed")])


def test_child_remove_is_private_and_missing_is_noop() -> None:
    parent = new_state([mk("b1", 1, "one"), mk("b2", 2, "two")])
    child = cow_fork(parent)
    assert child_remove(child, "missing") is child
    smaller = child_remove(child, "b1")
    assert [b.block_id for b in smaller.blocks] == ["b2"]
    assert smaller.parent_state_id == parent.state_id
    assert [b.block_id for b in parent.blocks] == ["b1", "b2"]


def test_fold_merges_and_dedupes() -> None:
    parent = new_state([mk("b1", 1, "one"), mk("b2", 2, "two")])
    fresh = mk("b3", 3, "three")
    child = child_add(cow_fork(parent), fresh)
    result = epoch_fold(child, parent)
    assert result.added == 1
    assert result.deduped == 2
    seq_ids = [(b.source_event_seq, b.block_id) for b in result.state.blocks]
    assert seq_ids == [(1, "b1"), (2, "b2"), (3, "b3")]
    assert result.state.blocks[2] is fresh


def test_fold_rejects_non_descendant() -> None:
    parent = new_state([mk("b1", 1, "one")])
    unrelated = new_state([mk("b9", 9, "nine")])
    with pytest.raises(FoldConflict, match="not-a-descendant"):
        epoch_fold(unrelated, parent)
    grandchild = cow_fork(cow_fork(parent))
    with pytest.raises(FoldConflict, match="not-a-descendant"):
        epoch_fold(grandchild, parent)


def test_fold_rejects_same_id_different_content() -> None:
    parent = new_state([mk("b1", 1, "one")])
    edited = child_add(child_remove(cow_fork(parent), "b1"), mk("b1", 1, "one rewritten"))
    with pytest.raises(FoldConflict) as exc:
        epoch_fold(edited, parent)
    assert "b1" in exc.value.detail


def test_fold_is_deterministic_and_empty_fold_is_noop() -> None:
    parent = new_state([mk("b1", 1, "one"), mk("b2", 2, "two")], watermark_event_seq=2)
    empty = epoch_fold(cow_fork(parent), parent)
    assert (empty.added, empty.deduped) == (0, 2)
    assert empty.state.state_id == parent.state_id
    edited = child_add(cow_fork(parent), mk("b3", 3, "three"))
    first, second = epoch_fold(edited, parent), epoch_fold(edited, parent)
    assert first.state.state_id == second.state.state_id
    assert (first.added, first.deduped) == (second.added, second.deduped)


def test_watermark_propagates_to_fold_result() -> None:
    parent = new_state([mk("b1", 1, "one")], watermark_event_seq=1)
    child = cow_fork(parent)
    assert child.watermark_event_seq == 1
    fresher = child.model_copy(update={"watermark_event_seq": 5})
    result = epoch_fold(fresher, parent)
    assert result.state.watermark_event_seq == 5
    assert parent.watermark_event_seq == 1
