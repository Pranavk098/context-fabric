"""Turn-level usage extraction from Claude Code transcript files.

Reads counts only (token totals and model ids), never prompt or tool-output
text, so a run record derived from these results cannot leak transcript content.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.plugin_ab.prices import dollar_cost

WEIGHT_CC = 1.25
WEIGHT_CR = 0.1
WEIGHT_OUT = 5.0


@dataclass
class ScanResult:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    turns: int = 0
    dollars: float | None = 0.0
    unpriced_models: set[str] = field(default_factory=set)

    def weighted(self) -> float:
        return (
            self.input_tokens
            + WEIGHT_CC * self.cache_creation_input_tokens
            + WEIGHT_CR * self.cache_read_input_tokens
            + WEIGHT_OUT * self.output_tokens
        )


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _iter_objects(path: Path) -> Any:
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                try:
                    obj = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _turn_tokens(usage: dict[str, Any]) -> tuple[int, int, int, int, int]:
    """Return ``(input, cc_5m, cc_1h, cache_read, output)`` for one usage block."""
    inp = _as_int(usage.get("input_tokens")) or 0
    out = _as_int(usage.get("output_tokens")) or 0
    cr = _as_int(usage.get("cache_read_input_tokens")) or 0
    nested = usage.get("cache_creation")
    if isinstance(nested, dict) and (
        "ephemeral_5m_input_tokens" in nested or "ephemeral_1h_input_tokens" in nested
    ):
        # Claude Code transcripts: usage.cache_creation.{ephemeral_5m,ephemeral_1h}_input_tokens
        n5 = _as_int(nested.get("ephemeral_5m_input_tokens")) or 0
        n1 = _as_int(nested.get("ephemeral_1h_input_tokens")) or 0
        return inp, n5, n1, cr, out
    cc5 = _as_int(usage.get("cache_creation_input_tokens_5m"))
    cc1 = _as_int(usage.get("cache_creation_input_tokens_1h"))
    if cc5 is not None or cc1 is not None:
        return inp, cc5 or 0, cc1 or 0, cr, out
    merged = _as_int(usage.get("cache_creation_input_tokens")) or 0
    return inp, merged, 0, cr, out


def scan_transcript(path: Path, prices: dict[str, Any]) -> ScanResult:
    """Aggregate token usage and dollars across one transcript's assistant turns."""
    result = ScanResult()
    for obj in _iter_objects(path):
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        model = msg.get("model")
        if not isinstance(model, str):
            model = ""
        inp, cc5, cc1, cr, out = _turn_tokens(usage)
        result.input_tokens += inp
        result.output_tokens += out
        result.cache_creation_input_tokens += cc5 + cc1
        result.cache_read_input_tokens += cr
        result.turns += 1
        if model:
            cost, unpriced = dollar_cost(
                prices,
                model,
                input_tokens=inp,
                cache_creation_5m=cc5,
                cache_creation_1h=cc1,
                cache_read=cr,
                output_tokens=out,
            )
            if unpriced is not None:
                result.unpriced_models.add(unpriced)
                result.dollars = None
            elif cost is not None and result.dollars is not None:
                result.dollars += cost
    return result


def combine(results: list[ScanResult]) -> ScanResult:
    total = ScanResult()
    for r in results:
        total.input_tokens += r.input_tokens
        total.output_tokens += r.output_tokens
        total.cache_creation_input_tokens += r.cache_creation_input_tokens
        total.cache_read_input_tokens += r.cache_read_input_tokens
        total.turns += r.turns
        total.unpriced_models |= r.unpriced_models
        if r.dollars is None:
            total.dollars = None
        elif total.dollars is not None:
            total.dollars += r.dollars
    return total
