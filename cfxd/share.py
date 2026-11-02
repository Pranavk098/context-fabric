"""``cf-share/2``: the paste-safe share payload (P2), built field by field.

The serializer copies a fixed list of names out of an aggregates mapping; it never
filters a richer object, so a new internal field cannot leak by accident. Both
renderers accept only a payload that passed :func:`validate_share`.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import cfxd

SCHEMA = "cf-share/2"
METRIC = "limit-weighted/1"
FAMILIES = ("opus", "sonnet", "haiku")

# Every cf-share/2 key, in output order. Nothing outside this tuple may leave the process.
FIELDS = (
    "schema",
    "plugin_version",
    "metric",
    "period_days",
    "sessions",
    "turns",
    "children",
    "children_by_model_family",
    "new_token_equivalents",
    "cache_read_tokens",
    "rediscovery_share_pct",
    "elisions",
    "elided_bytes",
    "consent_aggregate_publish",
)
_COUNTS = (
    "period_days",
    "sessions",
    "turns",
    "children",
    "cache_read_tokens",
    "elisions",
    "elided_bytes",
)
_OPTIONAL = ("new_token_equivalents", "rediscovery_share_pct")  # null means "not computed here"
_TEXT = ("schema", "plugin_version", "metric")
# A version tag, never free text: no spaces, slashes or path separators. Bounded to 32 chars.
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+!_-]{0,31}")


def _plugin_version() -> str:
    try:
        return version("context-fabric")
    except PackageNotFoundError:  # a source tree with no installed distribution
        return cfxd.__version__


def build_share(aggregates: Mapping[str, Any]) -> dict[str, Any]:
    """Copy the allowlisted fields out of ``aggregates``; unknown keys are never read."""
    families = aggregates.get("children_by_model_family")
    families = families if isinstance(families, Mapping) else {}
    return {
        "schema": SCHEMA,
        "plugin_version": _plugin_version(),
        "metric": METRIC,
        "period_days": aggregates.get("period_days", 0),
        "sessions": aggregates.get("sessions", 0),
        "turns": aggregates.get("turns", 0),
        "children": aggregates.get("children", 0),
        "children_by_model_family": {family: families.get(family, 0) for family in FAMILIES},
        # W1-03 computes the limit-weighted value; null until it lands.
        "new_token_equivalents": aggregates.get("new_token_equivalents"),
        "cache_read_tokens": aggregates.get("cache_read_tokens", 0),
        "rediscovery_share_pct": aggregates.get("rediscovery_share_pct"),
        "elisions": aggregates.get("elisions", 0),
        "elided_bytes": aggregates.get("elided_bytes", 0),
        "consent_aggregate_publish": aggregates.get("consent_aggregate_publish", False),
    }


def _number(key: str, value: Any) -> None:
    # bool is an int subclass, so the type test has to name it.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"cf-share/2: {key} must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"cf-share/2: {key} must be finite")


def _count(key: str, value: Any) -> None:
    """A count is a non-negative whole number; a float like 3.5 is not a count."""
    _number(key, value)
    if value < 0:
        raise ValueError(f"cf-share/2: {key} must not be negative")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"cf-share/2: {key} must be a whole number")


def validate_share(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``payload`` as a plain dict when it is exactly cf-share/2, else raise ``ValueError``.

    Messages name the offending field, never its value: a bad value may be a secret.
    """
    missing = sum(1 for key in FIELDS if key not in payload)
    unknown = sum(1 for key in payload if key not in FIELDS)
    if missing or unknown:
        raise ValueError(f"cf-share/2: {unknown} unknown key(s), {missing} missing key(s)")
    if payload["schema"] != SCHEMA:
        raise ValueError("cf-share/2: wrong schema")
    for key in _TEXT:
        if not isinstance(payload[key], str):
            raise ValueError(f"cf-share/2: {key} must be a string")
    if payload["metric"] != METRIC:
        raise ValueError(f"cf-share/2: metric must be {METRIC}")
    if not _VERSION.fullmatch(payload["plugin_version"]):
        raise ValueError("cf-share/2: plugin_version must be a version tag")
    for key in _COUNTS:
        _count(key, payload[key])
    families = payload["children_by_model_family"]
    if not isinstance(families, Mapping) or set(families) != set(FAMILIES):
        raise ValueError("cf-share/2: children_by_model_family must be exactly opus/sonnet/haiku")
    for family, count in families.items():
        _count(f"children_by_model_family.{family}", count)
    for key in _OPTIONAL:
        if payload[key] is None:
            continue
        _number(key, payload[key])
        if payload[key] < 0:
            raise ValueError(f"cf-share/2: {key} must not be negative")
    pct = payload["rediscovery_share_pct"]
    if pct is not None and pct > 100:
        raise ValueError("cf-share/2: rediscovery_share_pct must be within 0..100")
    if not isinstance(payload["consent_aggregate_publish"], bool):
        raise ValueError("cf-share/2: consent_aggregate_publish must be a boolean")
    return dict(payload)


def _number_text(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) else f"{value:,.2f}"


def _maybe_text(value: Any) -> str:
    return "n/a" if value is None else _number_text(value)


def _pct_text(value: Any) -> str:
    return "n/a" if value is None else f"{_number_text(value)}%"


def render_share_text(payload: Mapping[str, Any]) -> str:
    """Plain-text cf-share/2, read from the validated payload only."""
    share = validate_share(payload)
    families = share["children_by_model_family"]
    lines = [
        f"{SCHEMA}, context-fabric {share['plugin_version']}, "
        f"last {_number_text(share['period_days'])} days",
        f"Metric definition: {share['metric']}",
        f"Sessions: {_number_text(share['sessions'])}, "
        f"turns: {_number_text(share['turns'])}, "
        f"children: {_number_text(share['children'])} "
        f"(opus {_number_text(families['opus'])}, "
        f"sonnet {_number_text(families['sonnet'])}, "
        f"haiku {_number_text(families['haiku'])})",
        f"New token equivalents: {_maybe_text(share['new_token_equivalents'])}",
        f"Cache-read tokens: {_number_text(share['cache_read_tokens'])}",
        f"Rediscovery share: {_pct_text(share['rediscovery_share_pct'])}",
        f"Elisions: {_number_text(share['elisions'])} "
        f"({_number_text(share['elided_bytes'])} bytes)",
        "Aggregate publish consent: " + ("yes" if share["consent_aggregate_publish"] else "no"),
        "Counts and model families only: no paths, file names, repo names, digests or timestamps.",
    ]
    return "\n".join(lines) + "\n"


def render_share_json(payload: Mapping[str, Any]) -> str:
    """``cf-share/2`` as indented JSON, read from the validated payload only."""
    return json.dumps(validate_share(payload), indent=2) + "\n"
