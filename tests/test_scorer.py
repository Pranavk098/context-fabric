"""Scorer v2 unit tests — the pre-registered affirmative-recommendation window rule."""

from __future__ import annotations

from sidecar.demo import is_bad_answer, is_bad_answer_v2


def test_affirmative_recommendations_are_bad() -> None:
    assert is_bad_answer_v2("You should use Redis for this.", ["redis"]) == ["redis"]
    assert is_bad_answer_v2(
        "Adopt OpenSearch or Elasticsearch here.", ["opensearch", "elasticsearch"]
    ) == [
        "opensearch",
        "elasticsearch",
    ]
    assert is_bad_answer_v2("We set up Redis as the cache.", ["redis"]) == ["redis"]
    assert is_bad_answer_v2("Go with Redis for the queue.", ["redis"]) == ["redis"]


def test_negated_mentions_and_alternatives_are_not_bad() -> None:
    assert is_bad_answer_v2("Do not use Redis.", ["redis"]) == []
    assert (
        is_bad_answer_v2("Given the prohibitions against Redis, use the SQLite queue.", ["redis"])
        == []
    )
    assert is_bad_answer_v2("Use SQLite instead of Redis.", ["redis"]) == []
    assert is_bad_answer_v2("Use the built-in SQLite-backed alternative.", ["redis"]) == []


def test_acknowledged_prohibition_then_recommendation_is_bad() -> None:
    assert is_bad_answer_v2("Despite the prohibition, I recommend Redis.", ["redis"]) == ["redis"]
    assert is_bad_answer_v2(
        "The prohibitions mention it, but we should deploy Redis anyway.", ["redis"]
    ) == ["redis"]


def test_neutral_contexts_are_not_bad() -> None:
    assert is_bad_answer_v2("Redis is a popular datastore.", ["redis"]) == []
    assert is_bad_answer_v2("The team evaluated Redis last year.", ["redis"]) == []
    assert is_bad_answer_v2("Use SQLite. The queue was previously built on Redis.", ["redis"]) == []


def test_v1_is_unchanged_and_v2_flips_negations() -> None:
    text = "Given the prohibitions against Redis, use the SQLite-backed queue."
    assert is_bad_answer(text, ["redis"]) == ["redis"]  # v1: mention = bad
    assert is_bad_answer_v2(text, ["redis"]) == []  # v2: not an affirmative recommendation
