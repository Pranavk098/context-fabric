"""Cost model tests — exact arithmetic, no network."""

from __future__ import annotations

import pytest

from sidecar.costs import (
    PRESETS,
    CostRates,
    break_even_calls,
    cache_write_tokens_for_request,
    cached_tokens_after_ttl,
    effective_cached_tokens,
    estimate_cost,
    parse_custom_rates,
    parse_price_spec,
    prefix_cache_policy,
    sidecar_compute_cost,
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


def test_prefix_cache_policy_has_provider_rules() -> None:
    openai = prefix_cache_policy("openai:gpt-4o")
    assert openai.prefix_min_tokens == 1024
    assert openai.ttl_seconds == 600
    assert openai.write_premium is None

    anthropic = prefix_cache_policy("anthropic:sonnet")
    assert anthropic.prefix_min_tokens == 1024
    assert anthropic.ttl_seconds == 300
    assert anthropic.write_premium == 1.25


def test_effective_cached_tokens_respects_minimum_threshold() -> None:
    policy = prefix_cache_policy("openai:gpt-4o")
    assert effective_cached_tokens(1024, 2000, policy=policy) == 1024
    assert effective_cached_tokens(1023, 2000, policy=policy) == 0
    assert effective_cached_tokens(3000, 2000, policy=policy) == 2000
    assert effective_cached_tokens(1024, 0, policy=policy) == 0


def test_cached_tokens_after_ttl_miss_returns_zero() -> None:
    assert cached_tokens_after_ttl(1000, idle_seconds=10, ttl_seconds=300) == 1000
    assert cached_tokens_after_ttl(1000, idle_seconds=301, ttl_seconds=300) == 0
    assert cached_tokens_after_ttl(1000, idle_seconds=300, ttl_seconds=300) == 1000


def test_cache_write_only_on_ttl_miss_or_first_write() -> None:
    anthropic = prefix_cache_policy("anthropic:sonnet")
    # Warm cache: no write.
    assert cache_write_tokens_for_request(1024, policy=anthropic, idle_seconds=10) == 0
    # TTL miss: rewrite the prefix.
    assert cache_write_tokens_for_request(1024, policy=anthropic, idle_seconds=400) == 1024
    # Below minimum: no write charge.
    assert cache_write_tokens_for_request(900, policy=anthropic, idle_seconds=400) == 0

    openai = prefix_cache_policy("openai:gpt-4o")
    assert cache_write_tokens_for_request(1024, policy=openai, idle_seconds=400) == 0


def test_write_premium_matches_published_anthropic_rate() -> None:
    """Anthropic 5m cache write is 1.25x input; Sonnet input $3/M, write $3.75/M."""
    breakdown = estimate_cost(
        "anthropic:sonnet",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        cached_tokens=0,
        cache_writes=1_000_000,
    )
    assert breakdown is not None
    assert breakdown.cache_write_usd == 3.75
    assert breakdown.cache_write_usd / breakdown.input_usd == pytest.approx(1.25)


def test_sidecar_compute_cost_arithmetic() -> None:
    assert sidecar_compute_cost(0.0, 10.0) == 0.0
    assert sidecar_compute_cost(0.3, 2.0) == pytest.approx(0.3 * 2.0 / 3600)
    assert sidecar_compute_cost(-1.0, 2.0) == 0.0


def test_break_even_calls_for_provider_scenarios() -> None:
    # Anthropic 5m: 1.25x write, 0.1x read -> caching pays off at 2 calls.
    assert break_even_calls(write_premium=1.25, read_multiplier=0.1) == 2
    # OpenAI gpt-4o: no write premium, 0.5x read -> also pays off at 2 calls.
    assert break_even_calls(write_premium=1.0, read_multiplier=0.5) == 2
    # Hypothetical cheap write / deep discount -> still 2 calls.
    assert break_even_calls(write_premium=1.01, read_multiplier=0.1) == 2


def test_break_even_calls_rejects_bad_multipliers() -> None:
    with pytest.raises(ValueError):
        break_even_calls(write_premium=0.9, read_multiplier=0.5)
    with pytest.raises(ValueError):
        break_even_calls(write_premium=1.0, read_multiplier=1.0)
    with pytest.raises(ValueError):
        break_even_calls(write_premium=float("inf"), read_multiplier=0.5)
