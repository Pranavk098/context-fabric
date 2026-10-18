"""Scorer v3 unit tests — historical failure cases plus crafted rule-boundary cases.

Rule set: ``docs/engineering/NEGATIVE-SCORER.md`` (§1 taxonomy F1–F7, §2 rules). Every
historical failure case (F1–F6) is asserted to flip correctly; the crafted cases pin
clause scoping, multi-word tools, unicode, and the documented limitations.
"""

from __future__ import annotations

from sidecar.negscore import (
    ScoreResult,
    is_bad_answer,
    is_bad_answer_v2,
    score_v1,
    score_v2,
    score_v3,
)

REDIS = ["redis"]


def test_f1_v1_negated_compliance_mention_flips_to_not_bad() -> None:
    text = "Given the prohibitions against Redis and Celery, use the SQLite-backed queue."
    assert score_v1(text, ["redis", "celery"]).bad is True  # v1: mention = bad
    assert score_v3(text, ["redis", "celery"]).bad is False  # v3: not a recommendation


def test_f2_v1_mere_mention_of_failed_approach_flips_to_not_bad() -> None:
    text = (
        "The SQLite-backed queue avoids the issues associated with Redis, "
        "which had a significant data loss during a failover event."
    )
    assert score_v1(text, REDIS).bad is True
    assert score_v3(text, REDIS).bad is False


def test_f3_heading_style_mention_is_bad_v2_missed() -> None:
    text = "**Redis**: A popular choice for in-memory caching driven by high performance."
    assert score_v2(text, REDIS).bad is False  # documented heading-style false negative
    assert score_v3(text, REDIS).bad is True


def test_f4_bullet_list_recommendations_are_bad_v2_missed() -> None:
    for text in (
        "1. **Redis Cache**: Fast reads and easy integration.",
        "- **Redis**: A popular choice for in-memory caching.",
        "2. **Redis Queue**: Redis provides an in-memory store for jobs.",
    ):
        assert score_v2(text, REDIS).bad is False  # documented bullet-list false negative
        assert score_v3(text, REDIS).bad is True


def test_f5_modal_phrasing_is_bad_v2_missed() -> None:
    text = "A distributed cache could be Redis or a similar system."
    assert score_v2(text, REDIS).bad is False  # no listed verb before the mention
    assert score_v3(text, REDIS).bad is True
    assert score_v3("This could be a distributed cache like Redis.", REDIS).bad is True


def test_f6_v2_window_false_positive_flips_to_not_bad() -> None:
    text = (
        "To improve performance, we can implement a caching layer using the store's "
        "built-in row cache instead of adding another caching layer like Redis or Memcached."
    )
    assert score_v2(text, ["redis", "memcached"]).bad is True  # `instead` outside 8-token window
    assert score_v3(text, ["redis", "memcached"]).bad is False


def test_acknowledged_prohibition_then_recommendation_is_bad() -> None:
    assert score_v3("Despite the prohibition, I recommend Redis.", REDIS).bad is True
    assert (
        score_v3("The prohibitions mention it, but we should deploy Redis anyway.", REDIS).bad
        is True
    )


def test_negated_and_alternative_mentions_are_not_bad() -> None:
    for text in (
        "Do not use Redis.",
        "We avoid Redis in production.",
        "Use SQLite instead of Redis.",
        "Redis was rejected after the failover.",
        "We ruled out Redis early.",
    ):
        assert score_v3(text, REDIS).bad is False, text


def test_neutral_mentions_are_not_bad() -> None:
    for text in (
        "Redis is a popular datastore.",
        "The team evaluated Redis last year.",
        "Use SQLite. The queue was previously built on Redis.",
        "Redis Cache is a common pattern in this space.",
    ):
        assert score_v3(text, REDIS).bad is False, text


def test_multiword_tool_names() -> None:
    assert score_v3("We should adopt Apache Kafka for the queue.", ["apache kafka"]).bad is True
    assert score_v3("We rejected Apache Kafka in 2025.", ["apache kafka"]).bad is False
    assert (
        score_v3("The cache could be Google Cloud Memorystore.", ["google cloud memorystore"]).bad
        is True
    )
    assert (
        score_v3("Unlike Apache Kafka, the SQLite queue needs no broker.", ["apache kafka"]).bad
        is False
    )


def test_unicode_heading_and_curly_apostrophe() -> None:
    assert score_v3("\u201cRedis\u201d \u2014 a fast in-memory store.", REDIS).bad is True
    assert score_v3("We don\u2019t use Redis for queues.", REDIS).bad is False


def test_empty_response_is_not_bad() -> None:
    for text in ("", "   \n\n", "No technologies named here."):
        result = score_v3(text, REDIS)
        assert result.bad is False
        assert result.evidence == []


def test_tool_named_twice_one_negated_one_not_is_bad() -> None:
    text = "We avoid Redis in production, but we should adopt Redis for this cache."
    result = score_v3(text, REDIS)
    assert result.bad is True
    assert any(item.startswith("redis:") for item in result.evidence)


def test_clause_boundaries_scope_negation() -> None:
    # `avoid` binds only its own clause; the second tool is recommended by a listed act.
    assert (
        score_v3(
            "We avoid Redis, but we should adopt Elasticsearch.", ["redis", "elasticsearch"]
        ).bad
        is True
    )
    # Redis is negated; "is a fine choice" is not a listed act (documented limitation).
    assert (
        score_v3(
            "We avoid Redis, but Elasticsearch is a fine choice.", ["redis", "elasticsearch"]
        ).bad
        is False
    )
    # Negation does not leak backwards across the clause boundary.
    assert score_v3("We use the built-in cache; Redis is not needed.", REDIS).bad is False


def test_phrase_acts_are_bad() -> None:
    assert score_v3("Set up Redis as the queue broker.", REDIS).bad is True
    assert score_v3("Go with Redis here.", REDIS).bad is True
    assert score_v3("Migrate to Redis for caching.", REDIS).bad is True


def test_compliance_markers_exempt_contrast_mentions_clause_scoped() -> None:
    # A recommendation act inside the dead-end contrast clause stays not-bad.
    assert (
        score_v3("The proposal that was rolled back was to use Redis for the queue.", REDIS).bad
        is False
    )
    # A compliance cue in a *different* clause does not exempt a fresh recommendation.
    assert score_v3("Per the prohibitions, we still use Prometheus.", ["prometheus"]).bad is True


def test_evidence_and_case_insensitivity() -> None:
    result = score_v3("We should USE REDIS for this.", ["Redis"])
    assert result.bad is True
    assert result.evidence[0].startswith("Redis:")
    assert "use" in result.evidence[0]
    assert score_v3("The rediscovered pattern is old.", REDIS).bad is False


def test_inverted_phrasing_is_a_documented_miss() -> None:
    # Pinned limitation (NEGATIVE-SCORER.md §4): modal/verb after the mention is not caught.
    assert score_v3("Redis would be a good choice for this.", REDIS).bad is False


def test_wrappers_delegate_to_sidecar_frozen_rules() -> None:
    text = "Given the prohibitions against Redis, use the SQLite-backed queue."
    assert score_v1(text, REDIS).evidence == is_bad_answer(text, REDIS)
    assert score_v2(text, REDIS).evidence == is_bad_answer_v2(text, REDIS)
    assert score_v1(text, REDIS) == ScoreResult(bad=True, evidence=["redis"])
    assert score_v3("Do not use Redis.", REDIS) == ScoreResult(bad=False, evidence=[])
