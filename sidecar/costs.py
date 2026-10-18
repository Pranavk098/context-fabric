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
    source: str
    checked: str


PRESETS: dict[str, _Preset] = {
    "openai:gpt-4o": {
        "input_per_mtok": 2.50,
        "output_per_mtok": 10.00,
        "cached_input_per_mtok": 1.25,
        "source": _OPENAI_PRICING,
        "checked": CHECKED,
    },
    "openai:gpt-4o-mini": {
        "input_per_mtok": 0.15,
        "output_per_mtok": 0.60,
        "cached_input_per_mtok": 0.075,
        "source": _OPENAI_PRICING,
        "checked": CHECKED,
    },
    "anthropic:sonnet": {
        "input_per_mtok": 3.00,
        "output_per_mtok": 15.00,
        "cached_input_per_mtok": 0.30,
        "cache_write_per_mtok": 3.75,
        "source": _ANTHROPIC_PRICING,
        "checked": CHECKED,
    },
    "anthropic:haiku": {
        "input_per_mtok": 1.00,
        "output_per_mtok": 5.00,
        "cached_input_per_mtok": 0.10,
        "cache_write_per_mtok": 1.25,
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
