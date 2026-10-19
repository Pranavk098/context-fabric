"""Offline tests for the pre-registered A4 concurrency harness (no model, no network).

Asserts the frozen objective set, the error classification (including context-length
rejections), the nearest-rank percentile, the vLLM gauge parser, and the per-repeat cache
tag. See ``docs/engineering/A4-CONCURRENCY-PREREGISTRATION.md``.
"""

from __future__ import annotations

import evals.a4_concurrency as a4


def test_objectives_are_32_distinct_symbols() -> None:
    assert len(a4._OBJECTIVE_SYMBOLS) == 32
    assert len(set(a4._OBJECTIVE_SYMBOLS)) == 32
    objectives = [a4.objective(i) for i in range(32)]
    assert len(set(objectives)) == 32


def test_classify_error() -> None:
    assert a4.classify_error(200, "") == "ok"
    assert a4.classify_error(0, "") == "client_error"
    assert (
        a4.classify_error(400, "This model's maximum context length is 32768 tokens")
        == "context_length"
    )
    assert a4.classify_error(500, "internal server error") == "http_error"


def test_percentile() -> None:
    assert a4.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0
    assert a4.percentile([1.0, 2.0, 3.0, 4.0], 99) == 4.0
    assert a4.percentile([], 50) == 0.0


def test_parse_gauges() -> None:
    text = (
        "vllm:kv_cache_usage_perc 12.5\n"
        "vllm:num_requests_running 4.0\n"
        "vllm:num_requests_waiting 8.0\n"
        "# some comment\n"
    )
    assert a4.parse_gauges(text) == {
        "kv_cache_usage_perc": 12.5,
        "num_requests_running": 4.0,
        "num_requests_waiting": 8.0,
    }


def test_cache_tag_is_fresh_per_repeat_and_arm() -> None:
    assert a4.cache_tag("raw", 16, 1) != a4.cache_tag("raw", 16, 2)
    assert a4.cache_tag("raw", 16, 1) != a4.cache_tag("compile", 16, 1)
    assert a4.cache_tag("raw", 16, 1) != a4.cache_tag("raw", 32, 1)
