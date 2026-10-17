"""Lexical recall scoring (deterministic, no embeddings)."""

from __future__ import annotations

import re

from protocol.models import ContextBlock

_WORD = re.compile(r"[a-z0-9_]+")


def tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def lexical_relevance(objective: str, block: ContextBlock) -> float:
    obj = set(tokenize(objective))
    if not obj:
        return 0.0
    text = " ".join(
        [
            block.content or "",
            " ".join(block.symbols_exported),
            " ".join(block.symbols_imported),
        ]
    )
    overlap = obj & set(tokenize(text))
    return len(overlap) / len(obj)


if __name__ == "__main__":
    from protocol.models import BlockKind, ContextBlock, Creator, CreatorType, Fidelity, MemoryTier

    b = ContextBlock(
        block_id="b",
        block_kind=BlockKind.fact,
        memory_tier=MemoryTier.register,
        content="The retrieval endpoint uses HNSW indexing",
        content_hash="sha256:x",
        token_estimate=5,
        source_event_seq=1,
        creator=Creator(type=CreatorType.tool, id="t"),
        fidelity=Fidelity.extract,
        trust_domain="t",
    )
    assert lexical_relevance("retrieval HNSW", b) == 1.0
    assert lexical_relevance("unrelated thing", b) == 0.0
    print("recall.py self-check: OK")
