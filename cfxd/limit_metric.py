"""The limit-weighted "new tokens" metric from P8. Compute only: no CLI, no copy.

A share of a plan window needs a token denominator per tier. Anthropic publishes no
token-denominated 5-hour or weekly limit for any plan (see ``data/limit_weights.toml``),
so :func:`limit_share` returns ``None`` instead of inventing one. The metric itself is
still defined and reproducible: the raw totals come back when every weight is 1.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

# Version tag for share payloads: tester data under other definitions is never pooled.
METRIC = "limit-weighted/1"

DATA_FILE = Path(__file__).with_name("data") / "limit_weights.toml"

# The two plan windows the headline names. TOML keys, then user-facing words.
WINDOWS = ("five_hour_tokens", "week_tokens")

# One unverified 1/67 measurement must not decide a conclusion on its own.
SENSITIVITY_W_READ = {"1/30": 1 / 30, "1/67": 1 / 67, "1/100": 1 / 100}


@dataclass(frozen=True)
class Weights:
    """Price ratios for P8: cache writes, cache reads, output, against new input."""

    w_write: float
    w_read: float
    w_output: float


def _as_int(value: Any) -> int:
    """Missing or malformed usage counts as 0, as in ``audit._weighted``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _read(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return data


def load_weights(path: Path = DATA_FILE) -> Weights:
    """Load the weight table; a missing weight raises rather than defaulting."""
    raw = _read(path)["weights"]
    return Weights(
        w_write=float(raw["w_write"]),
        w_read=float(raw["w_read"]),
        w_output=float(raw["w_output"]),
    )


def load_limits(path: Path = DATA_FILE) -> dict[str, dict[str, int]]:
    """Load tier -> window -> denominator; a tier with no official figure stays empty.

    A denominator is refused unless the tier names an official ``source``, an
    ``as_of`` date and ``basis = "estimate"`` (decision D-3).
    """
    tiers: dict[str, Any] = _read(path).get("tier", {})
    limits: dict[str, dict[str, int]] = {}
    for tier, entry in tiers.items():
        present = {w: int(entry[w]) for w in WINDOWS if w in entry}
        if present and not (
            entry.get("source") and entry.get("as_of") and entry.get("basis") == "estimate"
        ):
            raise ValueError(
                f"tier {tier!r}: a denominator needs source, as_of and basis = 'estimate'"
            )
        limits[tier] = present
    return limits


def limit_weighted_new_tokens(usage: dict[str, Any], weights: Weights) -> float:
    """P8 formula: new input + cache writes*w_write + cache reads*w_read + output*w_output."""
    return (
        _as_int(usage.get("input_tokens"))
        + _as_int(usage.get("cache_creation_input_tokens")) * weights.w_write
        + _as_int(usage.get("cache_read_input_tokens")) * weights.w_read
        + _as_int(usage.get("output_tokens")) * weights.w_output
    )


def limit_share(
    usage: dict[str, Any],
    tier: str,
    window: str,
    weights: Weights | None = None,
    limits: dict[str, dict[str, int]] | None = None,
) -> float | None:
    """Share of one plan window, or ``None`` when the tier has no official denominator."""
    if window not in WINDOWS:
        raise ValueError(f"unknown window {window!r}; expected one of {WINDOWS}")
    denominator = (limits if limits is not None else load_limits()).get(tier, {}).get(window)
    if not denominator:
        return None
    return limit_weighted_new_tokens(usage, weights or load_weights()) / denominator


def sensitivity(usage: dict[str, Any], weights: Weights | None = None) -> dict[str, float]:
    """The headline at w_read = 1/30, 1/67, 1/100: a wrong ratio cannot flip a result silently."""
    base = weights or load_weights()
    return {
        label: limit_weighted_new_tokens(usage, replace(base, w_read=ratio))
        for label, ratio in SENSITIVITY_W_READ.items()
    }
