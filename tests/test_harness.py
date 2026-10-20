"""Tests for ``evals/harness.py`` telemetry (metrics-scraper failure handling)."""

from __future__ import annotations

from typing import Any

import pytest

import evals.harness as harness
from evals.harness import MetricsScraper


def test_metrics_scraper_records_failure_instead_of_silent_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise ConnectionError("metrics endpoint down")

    monkeypatch.setattr(harness.httpx, "get", boom)
    scraper = MetricsScraper("http://127.0.0.1:9/metrics")
    assert scraper.cache() == (0.0, 0.0)
    assert scraper.scrape_failed is True


def test_metrics_scraper_success_clears_failure_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 200
        text = "vllm:prefix_cache_hits_total 3.0\nvllm:prefix_cache_queries_total 4.0\n"

    monkeypatch.setattr(harness.httpx, "get", lambda *a, **k: _Resp())
    scraper = MetricsScraper("http://127.0.0.1:9/metrics")
    assert scraper.cache() == (3.0, 4.0)
    assert scraper.scrape_failed is False


def test_metrics_scraper_non_200_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 500
        text = ""

    monkeypatch.setattr(harness.httpx, "get", lambda *a, **k: _Resp())
    scraper = MetricsScraper("http://127.0.0.1:9/metrics")
    assert scraper.cache() == (0.0, 0.0)
    assert scraper.scrape_failed is True
