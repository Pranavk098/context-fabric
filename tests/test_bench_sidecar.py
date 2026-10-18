"""Fast smoke test for the offline sidecar bench harness (``evals/bench_sidecar.py``).

Runs one tiny matrix cell (N=4, two requests per client) against the in-process fake
upstream and checks the report shape and sanity of the numbers. The full matrix
(N in {1, 4, 16, 64}) is run by hand via ``uv run python -m evals.bench_sidecar``; it is
deliberately not part of the test suite.
"""

from __future__ import annotations

from evals.bench_sidecar import run


def test_bench_sidecar_smoke() -> None:
    report = run(concurrency=(4,), requests=2, blocks=8)
    assert report["scenario"] == "bench_sidecar"
    assert report["config"]["requests_per_client"] == 2
    assert report["config"]["fabric_blocks"] == 8
    assert set(report["modes"]) == {"passthrough", "compile"}
    for mode in ("passthrough", "compile"):
        stats = report["modes"][mode]["4"]
        assert stats["requests"] == 8
        assert stats["errors"] == 0
        assert stats["error_kinds"] == {}
        assert stats["rps"] > 0
        assert stats["wall_s"] > 0
        assert 0 < stats["p50_ms"] <= stats["p95_ms"] <= stats["p99_ms"]
    assert report["overhead"] is None  # overhead needs N=1, which this smoke skips
