"""Shared Okapi BM25 core (k1=1.2, b=0.75).

Single implementation used by ``kernel.router.recall`` (over ``ContextBlock`` records) and
``kernel.router.skim`` (over raw source strings), so a future k1/b/IDF change cannot
silently drift one scorer from the other (the "same scorer" claim depends on this).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence

BM25_K1 = 1.2
BM25_B = 0.75


def idf(n: int, df: int) -> float:
    """Positive Robertson-Spark-Jones IDF as used by Okapi BM25 (always > 0)."""
    return math.log(1.0 + (n - df + 0.5) / (df + 0.5))


def bm25_core(
    query_tokens: Sequence[str], tokenized_docs: Sequence[Sequence[str]]
) -> tuple[list[float], float]:
    """Per-doc BM25 scores plus the mean IDF of query terms present in the corpus.

    ``query_tokens`` must already be sorted and deduplicated (stable accumulation order);
    ``tokenized_docs`` are the token lists for each candidate doc. Returns
    ``(scores, mean_idf)``; scores are ``[0.0] * n`` and ``mean_idf`` is ``0.0`` when
    nothing matches.
    """
    n = len(tokenized_docs)
    if not query_tokens or n == 0:
        return [0.0] * n, 0.0
    lens = [len(d) for d in tokenized_docs]
    total = sum(lens)
    if total == 0:
        return [0.0] * n, 0.0
    avgdl = total / n
    df = Counter(t for d in tokenized_docs for t in set(d))
    present = [t for t in query_tokens if df[t] > 0]
    if not present:
        return [0.0] * n, 0.0
    mean_idf = sum(idf(n, df[t]) for t in present) / len(present)
    scores: list[float] = []
    for d in tokenized_docs:
        tf = Counter(d)
        dl = len(d)
        score = 0.0
        for t in present:  # sorted -> deterministic accumulation order
            if t in tf:
                score += (
                    idf(n, df[t])
                    * (tf[t] * (BM25_K1 + 1.0))
                    / (tf[t] + BM25_K1 * (1.0 - BM25_B + BM25_B * dl / avgdl))
                )
        scores.append(score)
    return scores, mean_idf
