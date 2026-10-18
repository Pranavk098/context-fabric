"""Published list-price presets and token-count cost arithmetic for demo receipts.

The table is a hand-maintained snapshot of public list prices (USD per million tokens),
``checked`` 2026-10-18. Providers change prices; verify before relying on a number. This
model multiplies token counts by list prices only — it does not model provider billing
quirks such as Anthropic cache TTLs or minimum charges.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import NotRequired, TypedDict

CHECKED = "2026-10-18"

_OPENAI_PRICING = "https://platform.openai.com/docs/pricing"
_ANTHROPIC_PRICING = "https://docs.anthropic.com/en/docs/about-claude/pricing"


class _Preset(TypedDict):
    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float
    cache_write_per_mtok: NotRequired[float]
    prefix_min_tokens: NotRequired[int]
    cache_ttl_seconds: NotRequired[int]
    cache_write_premium: NotRequired[float]
    source: str
    checked: str


PRESETS: dict[str, _Preset] = {
    "openai:gpt-4o": {
        "input_per_mtok": 2.50,
        "output_per_mtok": 10.00,
        "cached_input_per_mtok": 1.25,
        # OpenAI does not publish a single fixed minimum for gpt-4o-class models;
        # 1,024 is the published threshold for GPT-5.6+ and a conservative proxy.
        "prefix_min_tokens": 1024,
        "cache_ttl_seconds": 600,
        "source": _OPENAI_PRICING,
        "checked": CHECKED,
    },
    "openai:gpt-4o-mini": {
        "input_per_mtok": 0.15,
        "output_per_mtok": 0.60,
        "cached_input_per_mtok": 0.075,
        "prefix_min_tokens": 1024,
        "cache_ttl_seconds": 600,
        "source": _OPENAI_PRICING,
        "checked": CHECKED,
    },
    "anthropic:sonnet": {
        "input_per_mtok": 3.00,
        "output_per_mtok": 15.00,
        "cached_input_per_mtok": 0.30,
        "cache_write_per_mtok": 3.75,
        "prefix_min_tokens": 1024,
        "cache_ttl_seconds": 300,
        "cache_write_premium": 1.25,
        "source": _ANTHROPIC_PRICING,
        "checked": CHECKED,
    },
    "anthropic:haiku": {
        "input_per_mtok": 1.00,
        "output_per_mtok": 5.00,
        "cached_input_per_mtok": 0.10,
        "cache_write_per_mtok": 1.25,
        "prefix_min_tokens": 1024,
        "cache_ttl_seconds": 300,
        "cache_write_premium": 1.25,
        "source": _ANTHROPIC_PRICING,
        "checked": CHECKED,
    },
    "local:free": {
        "input_per_mtok": 0.0,
        "output_per_mtok": 0.0,
        "cached_input_per_mtok": 0.0,
        "source": "n/a (self-hosted; no per-token price)",
        "checked": CHECKED,
    },
}


@dataclass(frozen=True)
class CostRates:
    """USD per million tokens."""

    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float
    cache_write_per_mtok: float | None = None


@dataclass(frozen=True)
class PrefixCachePolicy:
    """Provider-specific prefix-cache rules used to model the competitor."""

    prefix_min_tokens: int
    ttl_seconds: int
    write_premium: float | None = None


@dataclass(frozen=True)
class CostBreakdown:
    """Per-component dollars plus the total for one call or arm."""

    input_usd: float
    cached_input_usd: float
    cache_write_usd: float
    output_usd: float
    total_usd: float
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int


def _preset_rates(preset: str) -> CostRates:
    try:
        entry = PRESETS[preset]
    except KeyError:
        known = ", ".join(PRESETS)
        raise ValueError(f"unknown price preset {preset!r}; known presets: {known}") from None
    return CostRates(
        input_per_mtok=entry["input_per_mtok"],
        output_per_mtok=entry["output_per_mtok"],
        cached_input_per_mtok=entry["cached_input_per_mtok"],
        cache_write_per_mtok=entry.get("cache_write_per_mtok"),
    )


def prefix_cache_policy(preset: str) -> PrefixCachePolicy:
    """Return the provider prefix-cache policy for a preset.

    ``prefix_min_tokens`` is the smallest shared prefix the provider will cache.
    ``ttl_seconds`` is the default/representative idle time after which a cached
    prefix becomes a miss. ``write_premium`` is the multiplier over the input
    rate for cache writes (``None`` when the provider has no separate write
    charge).
    """
    entry = PRESETS[preset]
    return PrefixCachePolicy(
        prefix_min_tokens=entry.get("prefix_min_tokens") or 1024,
        ttl_seconds=entry.get("cache_ttl_seconds") or 300,
        write_premium=entry.get("cache_write_premium"),
    )


def effective_cached_tokens(
    prefix_tokens: int,
    request_tokens: int,
    *,
    policy: PrefixCachePolicy,
) -> int:
    """Tokens that actually receive cached-read pricing after the provider gate.

    A prefix shorter than ``policy.prefix_min_tokens`` is not cached, so the
    discount is zero even when the provider would otherwise reuse it.
    """
    if prefix_tokens < policy.prefix_min_tokens or request_tokens <= 0:
        return 0
    return min(prefix_tokens, request_tokens)


def cached_tokens_after_ttl(
    cached_tokens: int,
    idle_seconds: float,
    ttl_seconds: float,
) -> int:
    """Return cached tokens that are still warm after ``idle_seconds`` of inactivity."""
    return cached_tokens if idle_seconds <= ttl_seconds else 0


def cache_write_tokens_for_request(
    prefix_tokens: int,
    *,
    policy: PrefixCachePolicy,
    idle_seconds: float = 0.0,
) -> int:
    """Tokens charged as a cache write on a request.

    For Anthropic this is the prefix size on the first request after a write or
    TTL miss; for OpenAI there is no separate write charge.
    """
    if policy.write_premium is None:
        return 0
    if prefix_tokens < policy.prefix_min_tokens:
        return 0
    if idle_seconds <= policy.ttl_seconds:
        # Warm cache: this request reads, it does not write.
        return 0
    # TTL miss: the prefix must be rewritten.
    return prefix_tokens


def sidecar_compute_cost(latency_seconds: float, cost_per_hour_usd: float) -> float:
    """Dollar cost of sidecar compile latency at a given compute price."""
    if latency_seconds <= 0 or cost_per_hour_usd <= 0:
        return 0.0
    return latency_seconds * cost_per_hour_usd / 3600.0


def break_even_calls(*, write_premium: float = 1.0, read_multiplier: float = 0.5) -> int:
    """Minimum calls inside one TTL for prefix caching to beat no prefix caching.

    Costs are expressed as multiples of the uncached input cost for the shared
    prefix. With caching: one write at ``write_premium`` plus ``N-1`` reads at
    ``read_multiplier``. Without caching: ``N`` full reads. Returns the smallest
    integer ``N`` where caching is cheaper.
    """
    if not math.isfinite(write_premium) or not math.isfinite(read_multiplier):
        raise ValueError("write_premium and read_multiplier must be finite")
    if read_multiplier >= 1.0:
        raise ValueError("read_multiplier must be < 1")
    if write_premium < 1.0:
        raise ValueError("write_premium must be >= 1.0")
    if write_premium <= read_multiplier:
        return 1
    return int(math.floor((write_premium - read_multiplier) / (1.0 - read_multiplier))) + 1


def _usd(tokens: int, per_mtok: float) -> float:
    return tokens * per_mtok / 1_000_000


def estimate_cost(
    preset: str | None,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    cache_writes: int = 0,
    custom: CostRates | None = None,
) -> CostBreakdown | None:
    """Estimate dollars for one call/arm; ``None`` when no price is configured.

    ``cached_tokens`` are billed at the cached-input rate and the remainder of
    ``prompt_tokens`` at the full input rate. ``cache_writes`` use the preset's
    cache-write rate when it has one, else the plain input rate. Counts are clamped
    only for cached tokens (to ``[0, prompt_tokens]``); negative counts are errors.
    """
    if custom is not None:
        if preset is not None:
            raise ValueError("pass either a preset name or custom rates, not both")
        rates = custom
    elif preset is not None:
        rates = _preset_rates(preset)
    else:
        return None
    for name, value in (
        ("prompt_tokens", prompt_tokens),
        ("completion_tokens", completion_tokens),
        ("cached_tokens", cached_tokens),
        ("cache_writes", cache_writes),
    ):
        if value < 0:
            raise ValueError(f"{name} must be non-negative, got {value}")
    cached = min(cached_tokens, prompt_tokens)
    write_rate = rates.cache_write_per_mtok
    if write_rate is None:
        write_rate = rates.input_per_mtok
    input_usd = _usd(prompt_tokens - cached, rates.input_per_mtok)
    cached_usd = _usd(cached, rates.cached_input_per_mtok)
    write_usd = _usd(cache_writes, write_rate)
    output_usd = _usd(completion_tokens, rates.output_per_mtok)
    total = input_usd + cached_usd + write_usd + output_usd
    return CostBreakdown(
        input_usd=input_usd,
        cached_input_usd=cached_usd,
        cache_write_usd=write_usd,
        output_usd=output_usd,
        total_usd=total,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached,
    )


def parse_custom_rates(spec: str) -> CostRates:
    """Parse ``custom:IN,OUT,CACHED[,CACHE_WRITE]`` (USD per million tokens)."""
    body = spec.removeprefix("custom:")
    if body == spec:
        raise ValueError("custom price must look like custom:IN,OUT,CACHED[,CACHE_WRITE]")
    parts = [part.strip() for part in body.split(",")]
    if len(parts) not in (3, 4):
        raise ValueError("custom price must have 3 or 4 comma-separated rates")
    try:
        values = [float(part) for part in parts]
    except ValueError:
        raise ValueError(f"custom price has a non-numeric rate: {body!r}") from None
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("custom price rates must be finite and non-negative")
    return CostRates(
        input_per_mtok=values[0],
        output_per_mtok=values[1],
        cached_input_per_mtok=values[2],
        cache_write_per_mtok=values[3] if len(values) == 4 else None,
    )


def parse_price_spec(spec: str) -> tuple[str | None, CostRates | None]:
    """Resolve ``--price`` to ``(preset, custom_rates)``; raises ``ValueError``."""
    if spec.startswith("custom:"):
        return None, parse_custom_rates(spec)
    if spec in PRESETS:
        return spec, None
    known = ", ".join(PRESETS)
    raise ValueError(
        f"unknown price preset {spec!r}; known presets: {known}, or custom:IN,OUT,CACHED[,WRITE]"
    )
