"""Dated price table and API-equivalent dollar computation.

Prices are read from ``prices.json`` (committed, dated). An unknown model is
reported as unpriced and never guessed. Claude Code transcripts merge 5-minute
and 1-hour cache-creation tokens into a single ``cache_creation_input_tokens``
field; the harness prices that merged value at the 5-minute write rate unless
the transcript carries explicit ``cache_creation_input_tokens_5m`` / ``_1h``
fields (documented in ``PROTOCOL.md``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PRICES_PATH = Path(__file__).with_name("prices.json")

_MTOK = 1_000_000.0


def load_prices(path: Path = PRICES_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        obj = json.load(fh)
    if not isinstance(obj, dict):
        raise ValueError("prices.json must be a JSON object")
    return obj


def normalize_model(model: str) -> str | None:
    """Map a Claude Code model id to a price-table key, or ``None`` when unpriced."""
    m = model.lower()
    if "opus" in m:
        return "opus-4.5"
    if "sonnet" in m and ("5.5" in m or "5-5" in m):
        return "sonnet-5.5"
    if "haiku" in m and ("5.5" in m or "5-5" in m):
        return "haiku-5.5"
    if "haiku" in m and ("4.5" in m or "4-5" in m):
        return "haiku-4.5"
    return None


def _select_tier(tiers: list[Any], prompt_tokens: int) -> dict[str, Any] | None:
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        cap = tier.get("max_prompt_tokens")
        if cap is None or prompt_tokens <= int(cap):
            return tier
    return None


def dollar_cost(
    prices: dict[str, Any],
    model: str,
    *,
    input_tokens: int,
    cache_creation_5m: int,
    cache_creation_1h: int,
    cache_read: int,
    output_tokens: int,
) -> tuple[float | None, str | None]:
    """Return ``(dollars, unpriced_model)`` for one turn.

    ``dollars`` is ``None`` and ``unpriced_model`` names the model when the model
    is absent from the price table; otherwise ``unpriced_model`` is ``None``.
    """
    key = normalize_model(model)
    if key is None:
        return None, model
    tiers = prices.get("models", {}).get(key)
    if not isinstance(tiers, list):
        return None, model
    prompt_tokens = input_tokens + cache_creation_5m + cache_creation_1h + cache_read
    tier = _select_tier(tiers, prompt_tokens)
    if tier is None:
        return None, model
    cost = (
        input_tokens * float(tier["input"])
        + cache_creation_5m * float(tier["cache_write_5m"])
        + cache_creation_1h * float(tier["cache_write_1h"])
        + cache_read * float(tier["cache_read"])
        + output_tokens * float(tier["output"])
    ) / _MTOK
    return cost, None
