"""P8 limit-weighted metric: formula, sensitivity, and the D-3 denominator rule.

The shipped table carries no denominator because Anthropic publishes no token figure
for any plan tier; the numeric path is exercised with a temporary table that has the
provenance D-3 requires. With every weight at 1 the metric must give back the raw
token totals of the golden transcripts, unchanged.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from cfxd.limit_metric import (
    DATA_FILE,
    METRIC,
    Weights,
    limit_share,
    limit_weighted_new_tokens,
    load_limits,
    load_weights,
    sensitivity,
)
from cfxd.usage_dedupe import MessageUsage

GOLDEN = Path(__file__).with_name("golden")
RAW_WEIGHTS = Weights(w_write=1.0, w_read=1.0, w_output=1.0)
USAGE = {
    "input_tokens": 1000,
    "cache_creation_input_tokens": 2000,
    "cache_read_input_tokens": 100_000,
    "output_tokens": 500,
}

OFFICIAL_TIER = """
[tier.demo]
five_hour_tokens = 2_000_000
week_tokens = 40_000_000
source = "https://example.invalid/official-usage"
as_of = "2026-11-02"
basis = "estimate"
"""


def _totals(path: Path) -> dict[str, int]:
    """Per-transcript component totals, usage counted once per message id."""
    text = path.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    dedupe = MessageUsage(rows)
    totals = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
    }
    for row in rows:
        msg: Any = row.get("message")
        if row.get("type") != "assistant" or not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict) or not dedupe.count_here(msg):
            continue
        final = dedupe.final_usage(msg, usage)
        for key in totals:
            totals[key] += final.get(key, 0)
    return totals


def test_formula_matches_p8() -> None:
    assert METRIC == "limit-weighted/1"
    assert limit_weighted_new_tokens(USAGE, RAW_WEIGHTS) == 103_500.0
    loaded = load_weights()
    expected = 1000 + 2000 * 1.25 + 100_000 * (1 / 67) + 500 * 5.0
    assert limit_weighted_new_tokens(USAGE, loaded) == pytest.approx(expected)
    # Missing usage fields count as zero, like audit._weighted.
    assert limit_weighted_new_tokens({}, loaded) == 0.0


def test_sensitivity_reruns_read_weight() -> None:
    weights = load_weights()
    table = sensitivity(USAGE, weights)
    assert list(table) == ["1/30", "1/67", "1/100"]
    assert table["1/30"] > table["1/67"] > table["1/100"]
    assert table["1/67"] == pytest.approx(
        1000 + 2000 * weights.w_write + 100_000 / 67 + 500 * weights.w_output
    )


def test_shipped_tiers_have_no_invented_denominator() -> None:
    shipped = tomllib.loads(DATA_FILE.read_text(encoding="utf-8"))
    tiers = shipped["tier"]
    assert {"pro", "max", "ultra"} <= set(tiers)
    for tier in tiers.values():
        assert tier["source"].startswith("https://")
        assert tier["as_of"]
        assert tier["basis"] == "estimate"
        assert "five_hour_tokens" not in tier
        assert "week_tokens" not in tier

    limits = load_limits()
    for name in limits:
        for window in ("five_hour_tokens", "week_tokens"):
            assert limit_share(USAGE, name, window, load_weights(), limits) is None


def test_limit_share_uses_an_official_denominator(tmp_path: Path) -> None:
    path = tmp_path / "limit_weights.toml"
    path.write_text(OFFICIAL_TIER, encoding="utf-8")
    limits = load_limits(path)
    # All-zero price weights reduce the metric to new input tokens: 1000.
    zero = Weights(w_write=0.0, w_read=0.0, w_output=0.0)
    assert limit_share(USAGE, "demo", "five_hour_tokens", zero, limits) == pytest.approx(1 / 2000)
    assert limit_share(USAGE, "demo", "week_tokens", zero, limits) == pytest.approx(1 / 40_000)
    # An absent window or an unknown tier has no denominator, not a default.
    assert limit_share(USAGE, "demo", "five_hour_tokens", zero, {}) is None
    assert limit_share(USAGE, "other", "five_hour_tokens", zero, limits) is None
    with pytest.raises(ValueError):
        limit_share(USAGE, "demo", "month_tokens", zero, limits)


def test_unprovenanced_denominator_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "limit_weights.toml"
    path.write_text("[tier.demo]\nfive_hour_tokens = 1000\n", encoding="utf-8")
    with pytest.raises(ValueError, match="official|source|estimate"):
        load_limits(path)


def test_all_weights_one_reproduces_golden_raw_totals() -> None:
    transcripts = sorted(GOLDEN.rglob("*.jsonl"))
    assert transcripts
    for path in transcripts:
        totals = _totals(path)
        raw = sum(totals.values())
        assert raw > 0
        assert limit_weighted_new_tokens(totals, RAW_WEIGHTS) == float(raw)
