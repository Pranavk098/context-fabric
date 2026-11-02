"""``cf-share/2`` (P2): allowlist serializer, validating renderers, canary fixture, goldens.

The leak fixture under ``tests/fixtures/leak_repo/`` carries a fake secret, absolute home
paths, a repo name and unique file names. Nothing in it may reach either render.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cfxd.share import FIELDS, build_share, render_share_json, render_share_text, validate_share

LEAK_REPO = Path(__file__).with_name("fixtures") / "leak_repo"
CANARIES = (
    "CANARYREPO-9009",
    "CANARYLEAKFILE-7001",
    "CANARYLEAKFILE-7002",
    "cfx_canary_sk_0000",
    "canary-home",
)


def _aggregates() -> dict[str, Any]:
    return {
        "period_days": 7,
        "sessions": 3,
        "turns": 42,
        "children": 2,
        "children_by_model_family": {"opus": 0, "sonnet": 2, "haiku": 0},
        # TODO(W1-03): limit-weighted metric lands here; until then the payload ships null.
        "cache_read_tokens": 1200,
        "elisions": 4,
        "elided_bytes": 812,
        "consent_aggregate_publish": True,
    }


def _fixture_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(LEAK_REPO.rglob("*.*")))


def _fixture_aggregates() -> dict[str, Any]:
    """Counts from the canary repo, plus the raw strings a richer aggregate object would hold."""
    files = sorted(path for path in LEAK_REPO.rglob("*") if path.is_file())
    text = _fixture_text()
    return {
        "period_days": 7,
        "sessions": len(files),
        "turns": text.count("\n"),
        "children": 1,
        "children_by_model_family": {"opus": 0, "sonnet": 1, "haiku": 0},
        "cache_read_tokens": 0,
        "elisions": 0,
        "elided_bytes": 0,
        "consent_aggregate_publish": True,
        "repo_name": "CANARYREPO-9009",
        "paths": [str(path) for path in files],
        "excerpt": text,
    }


def test_payload_is_exactly_the_cf_share_2_fields_in_order() -> None:
    payload = build_share(_aggregates())

    assert tuple(payload) == FIELDS
    assert payload["schema"] == "cf-share/2"
    assert payload["metric"] == "limit-weighted/1"
    assert payload["children_by_model_family"] == {"opus": 0, "sonnet": 2, "haiku": 0}
    # TODO(W1-03): the limit-weighted value; null until that work lands.
    assert payload["new_token_equivalents"] is None
    assert payload["rediscovery_share_pct"] is None


def test_unknown_aggregate_key_changes_nothing() -> None:
    before = build_share(_aggregates())
    richer = _aggregates() | {
        "prompts": ["CANARYPROMPT-6000"],
        "cost_usd": 12.5,
        "paths": ["/home/canary-home/CANARYREPO-9009/main.py"],
    }

    assert build_share(richer) == before


def test_unknown_payload_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown key"):
        validate_share(build_share(_aggregates()) | {"repo_name": "CANARYREPO-9009"})


def test_non_numeric_value_is_rejected() -> None:
    with pytest.raises(ValueError, match="sessions must be a number"):
        validate_share(build_share(_aggregates() | {"sessions": "3 files"}))


def test_boolean_is_rejected_outside_consent() -> None:
    with pytest.raises(ValueError, match="sessions must be a number"):
        validate_share(build_share(_aggregates() | {"sessions": True}))
    with pytest.raises(ValueError, match="consent_aggregate_publish must be a boolean"):
        validate_share(build_share(_aggregates() | {"consent_aggregate_publish": "yes"}))


def test_text_field_that_is_not_a_string_is_rejected() -> None:
    with pytest.raises(ValueError, match="metric must be a string"):
        validate_share(build_share(_aggregates()) | {"metric": 1})


def test_model_families_must_be_exactly_the_three() -> None:
    missing = build_share(_aggregates())
    missing["children_by_model_family"] = {"opus": 1}
    extra = build_share(_aggregates())
    extra["children_by_model_family"] = {"opus": 1, "sonnet": 1, "haiku": 1, "fable": 1}

    with pytest.raises(ValueError, match="children_by_model_family"):
        validate_share(missing)
    with pytest.raises(ValueError, match="children_by_model_family"):
        validate_share(extra)


def test_an_unknown_model_family_is_dropped_by_the_serializer() -> None:
    payload = build_share(_aggregates() | {"children_by_model_family": {"opus": 1, "fable": 1}})

    assert payload["children_by_model_family"] == {"opus": 1, "sonnet": 0, "haiku": 0}


def test_both_renderers_reject_a_payload_that_did_not_validate() -> None:
    bad = build_share(_aggregates()) | {"prompts": ["CANARYPROMPT-6000"]}
    with pytest.raises(ValueError):
        render_share_text(bad)
    with pytest.raises(ValueError):
        render_share_json(bad)


def test_canary_fixture_never_reaches_either_render() -> None:
    aggregates = _fixture_aggregates()
    on_disk = _fixture_text() + " ".join(aggregates["paths"])
    for canary in CANARIES:
        assert canary in on_disk, "fixture lost its canary"

    payload = build_share(aggregates)
    rendered = render_share_text(payload) + render_share_json(payload)

    for canary in CANARIES:
        assert canary not in rendered


def test_text_and_json_golden() -> None:
    payload = build_share(_aggregates())
    version = payload["plugin_version"]

    assert render_share_text(payload) == (
        f"cf-share/2, context-fabric {version}, last 7 days\n"
        "Metric definition: limit-weighted/1\n"
        "Sessions: 3, turns: 42, children: 2 (opus 0, sonnet 2, haiku 0)\n"
        "New token equivalents: n/a\n"
        "Cache-read tokens: 1,200\n"
        "Rediscovery share: n/a\n"
        "Elisions: 4 (812 bytes)\n"
        "Aggregate publish consent: yes\n"
        "Counts and model families only: no paths, file names, repo names, digests or timestamps.\n"
    )
    assert render_share_json(payload) == (
        "{\n"
        '  "schema": "cf-share/2",\n'
        f'  "plugin_version": "{version}",\n'
        '  "metric": "limit-weighted/1",\n'
        '  "period_days": 7,\n'
        '  "sessions": 3,\n'
        '  "turns": 42,\n'
        '  "children": 2,\n'
        '  "children_by_model_family": {\n'
        '    "opus": 0,\n'
        '    "sonnet": 2,\n'
        '    "haiku": 0\n'
        "  },\n"
        '  "new_token_equivalents": null,\n'
        '  "cache_read_tokens": 1200,\n'
        '  "rediscovery_share_pct": null,\n'
        '  "elisions": 4,\n'
        '  "elided_bytes": 812,\n'
        '  "consent_aggregate_publish": true\n'
        "}\n"
    )
    assert json.loads(render_share_json(payload)) == payload
