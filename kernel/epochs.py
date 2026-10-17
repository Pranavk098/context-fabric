"""Copy-on-write shared memory: COW_FORK, child-private edits, explicit EPOCH_FOLD.

A ``MemoryState`` is a frozen snapshot over immutable ``ContextBlock`` objects. ``cow_fork``
hands a child the same block objects (no copies); child edits return new child states and
never touch the parent; ``epoch_fold`` is the only merge point and rejects conflicts instead
of resolving them. ``state_id`` is deterministic: sha256 over the sorted block content hashes
plus the fork point, so identical operations yield identical ids.

v0 limits (see docs/engineering/SHARED-COPY.md): fold is add-only — a child's removals do not
propagate; tombstones are implicit (the block is simply absent from the child); folds are
immediate-descendant only (no sibling merging); no locking — one writer per child assumed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, model_validator

from protocol.canonical import canonicalize, sha256_hex
from protocol.models import ContextBlock

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class FoldConflict(Exception):
    """A fold or child edit rejected for conflict/lineage reasons; ``detail`` says why."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _order(blocks: Sequence[ContextBlock]) -> tuple[ContextBlock, ...]:
    return tuple(sorted(blocks, key=lambda b: (b.source_event_seq, b.block_id)))


def _state_id(parent_state_id: str | None, blocks: Sequence[ContextBlock]) -> str:
    payload = sorted(b.content_hash for b in blocks) + [parent_state_id or ""]
    return sha256_hex(canonicalize(payload))


class MemoryState(BaseModel):
    """Frozen snapshot of shared blocks; ``state_id`` covers content hashes + fork point."""

    model_config = _FROZEN

    state_id: str
    parent_state_id: str | None = None
    blocks: tuple[ContextBlock, ...] = ()
    watermark_event_seq: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _canonical(self) -> MemoryState:
        if self.blocks != _order(self.blocks):
            raise ValueError("blocks must be sorted by (source_event_seq, block_id)")
        ids = [b.block_id for b in self.blocks]
        if len(set(ids)) != len(ids):
            raise ValueError("blocks must have unique block_id values")
        if self.state_id != _state_id(self.parent_state_id, self.blocks):
            raise ValueError("state_id must match lineage + block content hashes")
        return self


def _make(
    parent_state_id: str | None, blocks: Sequence[ContextBlock], watermark_event_seq: int
) -> MemoryState:
    ordered = _order(blocks)
    return MemoryState(
        state_id=_state_id(parent_state_id, ordered),
        parent_state_id=parent_state_id,
        blocks=ordered,
        watermark_event_seq=watermark_event_seq,
    )


def new_state(blocks: Sequence[ContextBlock], watermark_event_seq: int = 0) -> MemoryState:
    """Root snapshot (no parent) over ``blocks``."""
    return _make(None, blocks, watermark_event_seq)


def cow_fork(state: MemoryState) -> MemoryState:
    """Child sharing the same immutable block objects; no block is copied."""
    return _make(state.state_id, state.blocks, state.watermark_event_seq)


def child_add(child: MemoryState, block: ContextBlock) -> MemoryState:
    """Child-local add. Identical (block_id, content_hash) -> no-op; same id, different
    content -> FoldConflict. Re-adding an id the child removed is allowed (edit path)."""
    for existing in child.blocks:
        if existing.block_id == block.block_id:
            if existing.content_hash == block.content_hash:
                return child
            raise FoldConflict(
                f"conflicting content for block_id {block.block_id!r}: "
                f"{existing.content_hash!r} != {block.content_hash!r}"
            )
    return _make(child.parent_state_id, (*child.blocks, block), child.watermark_event_seq)


def child_remove(child: MemoryState, block_id: str) -> MemoryState:
    """Child-local tombstone: the id is absent from the returned state; parent untouched.
    Missing id -> no-op. v0: removals never propagate through ``epoch_fold``."""
    remaining = tuple(b for b in child.blocks if b.block_id != block_id)
    if len(remaining) == len(child.blocks):
        return child
    return _make(child.parent_state_id, remaining, child.watermark_event_seq)


@dataclass(frozen=True)
class FoldResult:
    state: MemoryState
    added: int
    deduped: int


def epoch_fold(child: MemoryState, parent: MemoryState) -> FoldResult:
    """Merge the child's additions into the parent — explicit, validated, add-only.

    ``child`` must be an immediate descendant of ``parent`` (else FoldConflict
    "not-a-descendant"). Blocks already in the parent are deduped by (block_id,
    content_hash); a block_id with differing content anywhere is a FoldConflict. The
    result's watermark is the max of both sides; its parent is the parent's fork point.
    """
    if child.parent_state_id != parent.state_id:
        raise FoldConflict(
            f"not-a-descendant: child.parent_state_id={child.parent_state_id!r} "
            f"!= parent.state_id={parent.state_id!r}"
        )

    seen_hash: dict[str, str] = {}
    for block in (*parent.blocks, *child.blocks):
        prior = seen_hash.get(block.block_id)
        if prior is not None and prior != block.content_hash:
            raise FoldConflict(
                f"conflicting content for block_id {block.block_id!r}: "
                f"{prior!r} != {block.content_hash!r}"
            )
        seen_hash[block.block_id] = block.content_hash

    merged = list(parent.blocks)
    known = {(b.block_id, b.content_hash) for b in parent.blocks}
    added = deduped = 0
    for block in child.blocks:
        key = (block.block_id, block.content_hash)
        if key in known:
            deduped += 1
            continue
        merged.append(block)
        known.add(key)
        added += 1

    watermark = max(parent.watermark_event_seq, child.watermark_event_seq)
    return FoldResult(
        state=_make(parent.parent_state_id, merged, watermark), added=added, deduped=deduped
    )


if __name__ == "__main__":
    from datetime import UTC, datetime

    from protocol.models import BlockKind, Creator, CreatorType, Fidelity, MemoryTier

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
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        )

    root = new_state([mk("b1", 1, "one"), mk("b2", 2, "two")], watermark_event_seq=2)
    child = cow_fork(root)
    assert child.parent_state_id == root.state_id
    assert child.blocks[0] is root.blocks[0], "fork must share block objects"
    edited = child_add(child, mk("b3", 3, "three"))
    assert len(root.blocks) == 2 and len(edited.blocks) == 3, "child edits must stay private"
    folded = epoch_fold(edited, root)
    assert folded.added == 1 and folded.deduped == 2, (folded.added, folded.deduped)
    assert [b.block_id for b in folded.state.blocks] == ["b1", "b2", "b3"]
    assert folded.state.watermark_event_seq == 2
    removed = child_remove(edited, "b2")
    assert len(removed.blocks) == 2 and len(root.blocks) == 2
    print("epochs.py self-check: OK (fork -> private edit -> isolation -> fold)")
