"""Dependency / contradiction / negative-knowledge closure.

Given a set of selected blocks and the relation graph, force-includes what is required:
- ``depends_on`` parents of selected blocks,
- resolves ``supersedes`` (drops the superseded block),
- force-includes both sides of an unresolved ``contradicts`` and emits a conflict alert,
- force-includes ``negative_knowledge`` blocks whose symbols intersect the selection.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from protocol.models import BlockKind, ConflictAlert, ContextBlock

Relation = tuple[str, str, str]  # (source_block_id, target_block_id, type)


@dataclass
class Closure:
    blocks: list[ContextBlock] = field(default_factory=list)
    alerts: list[ConflictAlert] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def close(
    selected: Iterable[ContextBlock],
    all_blocks: dict[str, ContextBlock],
    relations: Iterable[Relation],
    objective: str,
) -> Closure:
    by_id = dict(all_blocks)
    chosen: dict[str, ContextBlock] = {b.block_id: b for b in selected}
    rels = list(relations)

    # depends_on: pull required parents
    changed = True
    while changed:
        changed = False
        for src, tgt, kind in rels:
            if kind == "depends_on" and src in chosen and tgt not in chosen and tgt in by_id:
                chosen[tgt] = by_id[tgt]
                changed = True

    # supersedes: drop the superseded target when the superseder is present
    for src, tgt, kind in rels:
        if kind == "supersedes" and src in chosen and tgt in chosen:
            del chosen[tgt]

    # contradicts: include both + alert
    for src, tgt, kind in rels:
        if kind == "contradicts" and (src in chosen) != (tgt in chosen):
            missing = tgt if src in chosen else src
            if missing in by_id:
                chosen[missing] = by_id[missing]

    # negative knowledge intersecting the selection's symbols or the objective
    obj_tokens = set(objective.lower().split())
    symbols: set[str] = set()
    for b in chosen.values():
        symbols |= set(b.symbols_exported) | set(b.symbols_imported)
    for b in by_id.values():
        if b.block_kind is not BlockKind.negative_knowledge:
            continue
        nk_syms = set(b.symbols_exported) | set(b.symbols_imported)
        mentions = nk_syms & obj_tokens or (b.content and any(s in b.content for s in symbols))
        if nk_syms & symbols or mentions:
            chosen[b.block_id] = b

    # now emit alerts for included contradictions
    alerts: list[ConflictAlert] = []
    for src, tgt, kind in rels:
        if kind == "contradicts" and src in chosen and tgt in chosen:
            alerts.append(ConflictAlert(a_block_id=src, b_block_id=tgt, reason="contradicts"))

    return Closure(blocks=[chosen[k] for k in sorted(chosen)], alerts=alerts)


if __name__ == "__main__":
    from protocol.models import Creator, CreatorType, Fidelity, MemoryTier

    def mk(bid: str, kind: BlockKind, seq: int, syms: tuple[str, ...] = ()) -> ContextBlock:
        return ContextBlock(
            block_id=bid,
            block_kind=kind,
            memory_tier=MemoryTier.ledger,
            content=f"content {bid}",
            content_hash="sha256:x",
            token_estimate=1,
            source_event_seq=seq,
            creator=Creator(type=CreatorType.agent, id="a"),
            fidelity=Fidelity.extract,
            trust_domain="t",
            symbols_exported=syms,
        )

    a = mk("a", BlockKind.fact, 1)
    dep = mk("dep", BlockKind.decision, 2)
    nk = mk("nk", BlockKind.negative_knowledge, 3, syms=("token",))
    by = {"a": a, "dep": dep, "nk": nk}
    c = close([a], by, [("a", "dep", "depends_on")], objective="token budget issue")
    ids = [b.block_id for b in c.blocks]
    assert "dep" in ids and "nk" in ids, ids
    print("dependency_closer.py self-check: OK")
