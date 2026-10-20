"""Recall tests — BM25 semantics, mode dispatch, optional embeddings (skip-guarded)."""

from __future__ import annotations

import pytest

from kernel.router import embeddings
from kernel.router._bm25 import BM25_B, BM25_K1
from kernel.router.recall import (
    CF_RECALL_ENV,
    _block_text,
    bm25_scores,
    lexical_relevance,
    relevance_scores,
    tokenize,
)
from protocol.models import (
    BlockKind,
    ContextBlock,
    Creator,
    CreatorType,
    Fidelity,
    MemoryTier,
)


def mk(
    bid: str, content: str, *, symbols: tuple[str, ...] = (), imports: tuple[str, ...] = ()
) -> ContextBlock:
    return ContextBlock(
        block_id=bid,
        block_kind=BlockKind.fact,
        memory_tier=MemoryTier.ledger,
        content=content,
        content_hash="sha256:" + bid,
        token_estimate=len(tokens(content)) or 1,
        source_event_seq=1,
        creator=Creator(type=CreatorType.agent, id="a"),
        fidelity=Fidelity.extract,
        trust_domain="t1",
        symbols_exported=symbols,
        symbols_imported=imports,
    )


def tokens(text: str) -> list[str]:
    return tokenize(text)


def _raise_import_error() -> None:
    raise ImportError("fastembed not installed")


def test_tokenize_is_lowercase_alnum_len2() -> None:
    assert tokenize("Hello, WORLD_x 7 a") == ["hello", "world"]
    assert tokenize("Foo_Bar/baz.qux-42") == ["foo", "bar", "baz", "qux", "42"]
    assert tokenize("") == []
    assert tokenize("!!!") == []


def test_bm25_params_are_okapi() -> None:
    assert (BM25_K1, BM25_B) == (1.2, 0.75)


def test_lexical_relevance_legacy_semantics() -> None:
    b = mk("b", "The retrieval endpoint uses HNSW indexing")
    assert lexical_relevance("retrieval HNSW", b) == 1.0
    assert lexical_relevance("retrieval missing", b) == 0.5
    assert lexical_relevance("unrelated thing", b) == 0.0
    assert lexical_relevance("", b) == 0.0
    assert lexical_relevance("hnsw", mk("s", "", symbols=("hnsw_index",))) == 1.0


def test_bm25_idf_rare_beats_common() -> None:
    corpus = [mk("d0", "common term")] + [mk(f"c{i}", "common filler note") for i in range(9)]
    corpus.append(mk("rare", "rare term"))
    rare_idf = bm25_scores("rare", corpus)["idf"]
    common_idf = bm25_scores("common", corpus)["idf"]
    assert rare_idf > common_idf > 0.0
    assert bm25_scores("rare", corpus)["rare"] > bm25_scores("common", corpus)["d0"]


def test_bm25_length_normalization_prefers_focused_doc() -> None:
    short = mk("short", "alpha beta")
    long = mk(
        "long",
        "alpha beta " + " ".join(f"filler{i}" for i in range(40)),
    )
    scores = bm25_scores("alpha beta", [short, long])
    assert scores["short"] > scores["long"] > 0.0


def test_bm25_deterministic_order_and_repeatability() -> None:
    corpus = [mk("b3", "delta"), mk("b1", "delta epsilon"), mk("b2", "delta zeta")]
    s1 = bm25_scores("delta zeta", corpus)
    s2 = bm25_scores("delta zeta", corpus)
    assert s1 == s2
    assert list(s1) == ["b3", "b1", "b2", "idf"]  # input order, corpus stat last


def test_bm25_empty_query_and_empty_corpus() -> None:
    corpus = [mk("a", "some content"), mk("b", "other content")]
    assert bm25_scores("", corpus) == {"a": 0.0, "b": 0.0, "idf": 0.0}
    assert bm25_scores("x", []) == {"idf": 0.0}
    assert bm25_scores("a", [mk("z", "")]) == {"z": 0.0, "idf": 0.0}


def test_bm25_zero_overlap_is_exactly_zero() -> None:
    corpus = [mk("a", "postgres storage"), mk("b", "redis cache")]
    scores = bm25_scores("kubernetes ingress", corpus)
    assert scores == {"a": 0.0, "b": 0.0, "idf": 0.0}


def test_bm25_score_positive_iff_query_token_present() -> None:
    corpus = [
        mk("a", "The retry backoff caps at thirty seconds"),
        mk("b", "Unrelated plumbing notes"),
        mk("c", "", symbols=("backoff_helper",)),
    ]
    query = "retry backoff"
    scores = bm25_scores(query, corpus)
    for block in corpus:
        lexical_hit = bool(set(tokenize(query)) & set(tokenize(_block_text(block))))
        assert (scores[block.block_id] > 0.0) == lexical_hit


def test_bm25_unicode_content() -> None:
    corpus = [mk("de", "Die München Route ist schnell"), mk("ru", "Обзор кафе на вокзале")]
    assert bm25_scores("München", corpus)["de"] > 0.0
    assert bm25_scores("münchen", corpus)["de"] == bm25_scores("MÜNCHEN", corpus)["de"]
    assert bm25_scores("кафе", corpus)["ru"] > 0.0
    assert bm25_scores("münchen", corpus)["ru"] == 0.0


def test_relevance_scores_defaults_to_bm25(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CF_RECALL_ENV, raising=False)
    corpus = [mk("a", "retry backoff overflow"), mk("b", "unrelated")]
    assert relevance_scores("retry backoff", corpus) == bm25_scores("retry backoff", corpus)


def test_relevance_scores_empty_blocks() -> None:
    assert relevance_scores("anything", []) == {}


def test_relevance_scores_rejects_unknown_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CF_RECALL_ENV, "telepathy")
    with pytest.raises(ValueError, match="telepathy"):
        relevance_scores("q", [mk("a", "content")])


def test_semantic_modes_fall_back_to_bm25_without_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embeddings, "_model", _raise_import_error)
    corpus = [mk("a", "retry backoff overflow"), mk("b", "unrelated")]
    expected = bm25_scores("retry backoff", corpus)
    for mode in ("embeddings", "hybrid"):
        assert relevance_scores("retry backoff", corpus, mode=mode) == expected


def test_embedding_scores_none_when_extra_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings, "_model", _raise_import_error)
    assert embeddings.embedding_scores("q", [mk("a", "content")]) is None
    assert embeddings.hybrid_scores("q", [mk("a", "content")]) is None


def test_hybrid_combines_half_half(monkeypatch: pytest.MonkeyPatch) -> None:
    corpus = [mk("a", "x"), mk("b", "y")]
    monkeypatch.setattr(embeddings, "embedding_scores", lambda q, bs: {"a": 0.2, "b": 0.4})
    monkeypatch.setattr(embeddings, "bm25_scores", lambda q, bs: {"a": 10.0, "b": 5.0, "idf": 1.0})
    # bm25 normalized {a: 1.0, b: 0.5}; embeddings normalized {a: 0.5, b: 1.0}
    assert embeddings.hybrid_scores("q", corpus) == {"a": 0.75, "b": 0.75}


def test_hybrid_keeps_block_whose_id_is_the_idf_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = [mk("idf", "canonical storage"), mk("z", "coffee")]
    monkeypatch.setattr(embeddings, "embedding_scores", lambda q, bs: {"idf": 0.9, "z": 0.1})
    scores = embeddings.hybrid_scores("canonical", corpus)
    assert scores is not None
    assert set(scores) == {"idf", "z"}
    assert scores["idf"] > scores["z"]


@pytest.mark.skipif(not embeddings.available(), reason="fastembed extra not installed")
def test_embedding_scores_rank_semantic_match_first() -> None:
    corpus = [
        mk("hit", "Content-addressed caching eliminates duplicate agent downloads"),
        mk("noise", "The office coffee machine is out of beans again"),
    ]
    scores = embeddings.embedding_scores("how to avoid fetching the same file twice", corpus)
    assert scores is not None
    assert scores["hit"] > scores["noise"]
    hybrid = embeddings.hybrid_scores("duplicate agent downloads", corpus)
    assert hybrid is not None and hybrid["hit"] > hybrid["noise"]


@pytest.mark.skipif(not embeddings.available(), reason="fastembed extra not installed")
def test_embedding_scores_deterministic() -> None:
    corpus = [mk("a", "deterministic onnx cpu inference"), mk("b", "unrelated note")]
    first = embeddings.embedding_scores("deterministic inference", corpus)
    second = embeddings.embedding_scores("deterministic inference", corpus)
    assert first is not None and first == second
