"""Cost model tests — exact arithmetic, no network."""

from __future__ import annotations

import pytest

from sidecar.costs import (
    PRESETS,
    CostRates,
    estimate_cost,
    parse_custom_rates,
    parse_price_spec,
)


def test_preset_table_is_complete() -> None:
    for name, entry in PRESETS.items():
        assert entry["input_per_mtok"] >= 0
        assert entry["output_per_mtok"] >= 0
        assert entry["cached_input_per_mtok"] >= 0
        assert entry["source"]
        assert entry["checked"] == "2026-10-18", name


def test_exact_arithmetic_custom_rates() -> None:
    rates = CostRates(
        input_per_mtok=2.5,
        output_per_mtok=10.0,
        cached_input_per_mtok=1.25,
        cache_write_per_mtok=3.75,
    )
    breakdown = estimate_cost(
        None,
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        cached_tokens=400_000,
        cache_writes=200_000,
        custom=rates,
    )
    assert breakdown is not None
    assert breakdown.input_usd == 1.5  # 600k uncached x $2.50/M
    assert breakdown.cached_input_usd == 0.5  # 400k cached x $1.25/M
    assert breakdown.cache_write_usd == 0.75  # 200k writes x $3.75/M
    assert breakdown.output_usd == 10.0  # 1M x $10/M
    assert breakdown.total_usd == 12.75
    assert breakdown.cached_tokens == 400_000


def test_cached_discount_path_uses_preset_cached_rate() -> None:
    breakdown = estimate_cost(
        "openai:gpt-4o", prompt_tokens=1_000_000, completion_tokens=0, cached_tokens=1_000_000
    )
    assert breakdown is not None
    assert breakdown.input_usd == 0.0
    assert breakdown.cached_input_usd == 1.25  # $1.25/M, not $2.50/M
    assert breakdown.total_usd == 1.25


def test_cache_write_defaults_to_input_rate_without_a_write_rate() -> None:
    breakdown = estimate_cost(
        "openai:gpt-4o", prompt_tokens=0, completion_tokens=0, cache_writes=1_000_000
    )
    assert breakdown is not None
    assert breakdown.cache_write_usd == 2.5
    assert breakdown.total_usd == 2.5


def test_cache_writes_use_preset_write_rate() -> None:
    breakdown = estimate_cost(
        "anthropic:sonnet", prompt_tokens=0, completion_tokens=0, cache_writes=1_000_000
    )
    assert breakdown is not None
    assert breakdown.cache_write_usd == 3.75


def test_cached_tokens_are_clamped_to_prompt_tokens() -> None:
    breakdown = estimate_cost(
        "openai:gpt-4o-mini", prompt_tokens=100, completion_tokens=0, cached_tokens=1_000
    )
    assert breakdown is not None
    assert breakdown.cached_tokens == 100
    assert breakdown.input_usd == 0.0


def test_custom_rates_parse_three_and_four_values() -> None:
    assert parse_custom_rates("custom:2.5,10,1.25") == CostRates(2.5, 10.0, 1.25, None)
    assert parse_custom_rates("custom:2.5,10,1.25,3.75") == CostRates(2.5, 10.0, 1.25, 3.75)


@pytest.mark.parametrize(
    "spec",
    [
        "custom:2.5,10",
        "custom:2.5,10,1.25,3.75,5",
        "custom:a,b,c",
        "2.5,10,1.25",
        "custom:-1,10,1.25",
        "custom:2.5,10,inf",
        "custom:2.5,10,nan",
    ],
)
def test_malformed_custom_rates_raise(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_custom_rates(spec)


def test_unknown_preset_raises() -> None:
    with pytest.raises(ValueError, match="unknown price preset"):
        estimate_cost("nope", prompt_tokens=1, completion_tokens=1)
    with pytest.raises(ValueError, match="unknown price preset"):
        parse_price_spec("nope")


def test_parse_price_spec_returns_preset_or_custom() -> None:
    assert parse_price_spec("openai:gpt-4o-mini") == ("openai:gpt-4o-mini", None)
    preset, custom = parse_price_spec("custom:2.5,10,1.25")
    assert preset is None
    assert custom == CostRates(2.5, 10.0, 1.25, None)


def test_none_and_zero_handling() -> None:
    assert estimate_cost(None, prompt_tokens=1_000_000, completion_tokens=1_000_000) is None
    breakdown = estimate_cost("openai:gpt-4o", prompt_tokens=0, completion_tokens=0)
    assert breakdown is not None
    assert breakdown.total_usd == 0.0
    free = estimate_cost(
        "local:free", prompt_tokens=1_000_000, completion_tokens=1_000_000, cache_writes=1_000_000
    )
    assert free is not None
    assert free.total_usd == 0.0


def test_negative_token_counts_raise() -> None:
    with pytest.raises(ValueError):
        estimate_cost("openai:gpt-4o", prompt_tokens=-1, completion_tokens=0)
