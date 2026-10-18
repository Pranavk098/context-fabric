"""Recall scoring: Okapi BM25 by default, optional embeddings via the ``embeddings`` extra.

Deterministic core (ADR-0003): the default path is pure Python BM25 over the candidate
block set — no services, no RNG, stable float accumulation order (query terms are summed
in sorted order). Semantic recall is opt-in through ``CF_RECALL`` (``bm25`` | ``hybrid`` |
``embeddings``) and falls back to BM25 when the optional extra is not installed.

Tokenization contract (shared by every mode): lowercase, split on any non-alphanumeric
character (Unicode-aware — accented/CJK runs survive), drop tokens shorter than 2
characters. Underscores are separators (``snake_case`` -> ``snake``, ``case``).
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from collections.abc import Sequence

from protocol.models import ContextBlock

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)

BM25_K1 = 1.2
BM25_B = 0.75

CF_RECALL_ENV = "CF_RECALL"
RECALL_MODES = ("bm25", "hybrid", "embeddings")


def tokenize(text: str) -> list[str]:
    """Deterministic tokenization (see module docstring)."""
    return [t for t in _TOKEN.findall(text.lower()) if len(t) >= 2]


def _block_text(block: ContextBlock) -> str:
    return " ".join([block.content or "", *block.symbols_exported, *block.symbols_imported])


def _block_tokens(block: ContextBlock) -> list[str]:
    return tokenize(_block_text(block))


def lexical_relevance(objective: str, block: ContextBlock) -> float:
    """Fraction of objective tokens present in the block (naive overlap, legacy callers).

    Kept for ``scorer.py``; it uses the tokenization contract above (underscores split,
    tokens shorter than 2 characters dropped), so it is not byte-identical to the pre-BM25
    ``[a-z0-9_]+`` tokenizer. New code should prefer :func:`bm25_scores` /
    :func:`relevance_scores`.
    """
    obj = set(tokenize(objective))
    if not obj:
        return 0.0
    overlap = obj & set(_block_tokens(block))
    return len(overlap) / len(obj)


def _idf(n: int, df: int) -> float:
    """Positive Robertson-Spark-Jones IDF as used by Okapi BM25 (always > 0)."""
    return math.log(1.0 + (n - df + 0.5) / (df + 0.5))


def bm25_scores(query: str, blocks: Sequence[ContextBlock]) -> dict[str, float]:
    """Okapi BM25 (k1=1.2, b=0.75) with corpus statistics over ``blocks``.

    Returns ``block_id -> score`` for every candidate plus a special ``"idf"`` entry:
    the mean IDF of query terms present in the corpus (0.0 when none). A block scores
    exactly 0.0 iff no query token occurs in it, so ``score > 0`` is a valid seed test.
    IDF is candidate-set relative by construction (rare in this corpus = informative).
    """
    result: dict[str, float] = {b.block_id: 0.0 for b in blocks}
    result["idf"] = 0.0
    terms = sorted(set(tokenize(query)))
    docs = [_block_tokens(b) for b in blocks]
    n = len(docs)
    if not terms or n == 0:
        return result
    lens = [len(d) for d in docs]
    total = sum(lens)
    if total == 0:
        return result
    avgdl = total / n
    df = Counter(t for d in docs for t in set(d))
    present = [t for t in terms if df[t] > 0]
    if not present:
        return result
    result["idf"] = sum(_idf(n, df[t]) for t in present) / len(present)
    for b, d, dl in zip(blocks, docs, lens, strict=True):
        tf = Counter(d)
        score = 0.0
        for t in present:  # sorted -> deterministic accumulation order
            if t in tf:
                score += (
                    _idf(n, df[t])
                    * (tf[t] * (BM25_K1 + 1.0))
                    / (tf[t] + BM25_K1 * (1.0 - BM25_B + BM25_B * dl / avgdl))
                )
        result[b.block_id] = score
    return result


def relevance_scores(
    query: str, blocks: Sequence[ContextBlock], *, mode: str | None = None
) -> dict[str, float]:
    """Single recall entry point; mode defaults to ``CF_RECALL`` (``bm25`` if unset).

    ``embeddings``/``hybrid`` degrade to BM25 when the ``embeddings`` extra is absent.
    """
    selected = mode if mode is not None else os.environ.get(CF_RECALL_ENV, "bm25")
    selected = selected.strip().lower()
    if selected not in RECALL_MODES:
        raise ValueError(
            f"unknown {CF_RECALL_ENV} mode {selected!r}; expected one of {RECALL_MODES}"
        )
    if not blocks:
        return {}
    if selected == "bm25":
        return bm25_scores(query, blocks)
    from kernel.router.embeddings import embedding_scores, hybrid_scores  # lazy: optional extra

    scores = (
        embedding_scores(query, blocks)
        if selected == "embeddings"
        else hybrid_scores(query, blocks)
    )
    return bm25_scores(query, blocks) if scores is None else scores


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

    b = _mk("b", "The retrieval endpoint uses HNSW indexing")
    assert lexical_relevance("retrieval HNSW", b) == 1.0
    assert lexical_relevance("unrelated thing", b) == 0.0
    corpus = [b, _mk("c", "A retrieval worker retries retrieval of the retrieval index")]
    scores = bm25_scores("retrieval HNSW", corpus)
    assert scores["b"] > scores["c"] > 0.0  # shared term + rare term beats repeated common-only
    assert bm25_scores("absent term", corpus)["b"] == 0.0
    assert tokenize("Hello, WORLD_x 7 a") == ["hello", "world"]
    print("recall.py self-check: OK")
