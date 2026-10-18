"""Optional semantic recall via fastembed (local ONNX, CPU, no network at scoring time).

Install the extra: ``uv sync --extra embeddings``. When fastembed is absent every
public function returns ``None`` and callers fall back to lexical BM25, so the kernel
core stays dependency-free. Model: ``BAAI/bge-small-en-v1.5`` (fastembed's default,
~130MB one-time download). CPU ONNX inference is deterministic for a fixed model snapshot,
but fastembed resolves the Hub revision at download time and this module passes no revision
pin, so a cold cache can fetch a newer snapshot (ADR-0003 replay caveat; see
``docs/engineering/RECALL-BENCH.md``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from kernel.router.recall import _block_text, bm25_scores
from protocol.models import ContextBlock

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"

HYBRID_BM25_WEIGHT = 0.5
HYBRID_EMBEDDING_WEIGHT = 0.5

_MODEL: Any = None


def available() -> bool:
    """True when the ``embeddings`` extra (fastembed) is importable."""
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return False
    return True


def _model() -> Any:
    """Lazily construct the shared TextEmbedding singleton (downloads on first use)."""
    global _MODEL
    if _MODEL is None:
        from fastembed import TextEmbedding

        _MODEL = TextEmbedding(model_name=DEFAULT_MODEL)
    return _MODEL


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na > 0.0 and nb > 0.0 else 0.0


def embedding_scores(query: str, blocks: Sequence[ContextBlock]) -> dict[str, float] | None:
    """Cosine similarity (query, block) per block; ``None`` when the extra is absent."""
    if not blocks:
        return {}
    try:
        model = _model()
    except ImportError:
        return None
    query_vec = next(iter(model.query_embed([query])))
    doc_vecs = list(model.embed([_block_text(b) for b in blocks]))
    return {b.block_id: _cosine(query_vec, dv) for b, dv in zip(blocks, doc_vecs, strict=True)}


def _max_normalized(scores: dict[str, float]) -> dict[str, float]:
    top = max((max(0.0, v) for v in scores.values()), default=0.0)
    if top <= 0.0:
        return dict.fromkeys(scores, 0.0)
    return {k: max(0.0, v) / top for k, v in scores.items()}


def hybrid_scores(query: str, blocks: Sequence[ContextBlock]) -> dict[str, float] | None:
    """``0.5 * max-normalized BM25 + 0.5 * max-normalized cosine`` per block.

    Both signals are scaled to [0, 1] over the candidate set (negative cosines clamped
    to 0), so neither dominates by raw magnitude. Returns ``None`` when the extra is
    absent. A block scores 0.0 iff it has no lexical match and non-positive similarity.
    """
    emb = embedding_scores(query, blocks)
    if emb is None:
        return None
    bm_all = bm25_scores(query, blocks)
    # Select by block id instead of popping the "idf" sentinel: a block may legitimately
    # have block_id "idf", and bm25_scores stores its real score under that key.
    bm = {b.block_id: bm_all[b.block_id] for b in blocks}
    bm_n = _max_normalized(bm)
    emb_n = _max_normalized(emb)
    return {
        bid: HYBRID_BM25_WEIGHT * bm_n[bid] + HYBRID_EMBEDDING_WEIGHT * emb_n[bid] for bid in bm
    }


if __name__ == "__main__":
    from protocol.models import BlockKind, ContextBlock, Creator, CreatorType, Fidelity, MemoryTier

    def _mk(bid: str, content: str) -> ContextBlock:
        return ContextBlock(
            block_id=bid,
            block_kind=BlockKind.fact,
            memory_tier=MemoryTier.register,
            content=content,
            content_hash="sha256:x",
            token_estimate=5,
            source_event_seq=1,
            creator=Creator(type=CreatorType.tool, id="t"),
            fidelity=Fidelity.extract,
            trust_domain="t",
        )

    corpus = [
        _mk("hit", "Content-addressed caching eliminates duplicate agent downloads"),
        _mk("miss", "The office coffee machine is out of beans again"),
    ]
    if not available():
        print("embeddings.py self-check: skipped (fastembed not installed)")
    else:
        scores = embedding_scores("how to avoid fetching the same file twice", corpus)
        assert scores is not None and scores["hit"] > scores["miss"]
        hybrid = hybrid_scores("duplicate agent downloads", corpus)
        assert hybrid is not None and hybrid["hit"] > hybrid["miss"]
        print("embeddings.py self-check: OK")
