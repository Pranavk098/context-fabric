"""Offline tests for the pre-registered B4 divergent-role harness (no model, no network).

Asserts the frozen arm construction (B's agents share identical leading bytes; C's packets
differ by role; A sends the full raw bundle), the required+forbidden scorer on hand-written
good/bad answers, and that the dry-run payload is byte-deterministic. See
``docs/engineering/B4-PREREGISTRATION.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from evals.b4_divergent import (
    _ARM_TAGS,
    AGENT_BY_ROLE,
    build_arms,
    dry_run_payload,
    fact_blocks,
    score_quality,
)

AS_OF = datetime(2026, 1, 1, tzinfo=UTC)

# A minimal stand-in for the raw bundle that carries every role's required fact (so the
# dry-run construction checks pass offline, without the pinned httpx clone).
SYNTHETIC_BUNDLE = (
    "===== FILE: httpx/__init__.py =====\n"
    "from ._client import Client, AsyncClient\n"
    "===== FILE: httpx/_api.py =====\n"
    "def get(url): return request('GET', url)\n"
    "def post(url): return request('POST', url)\n"
    "===== FILE: httpx/__version__.py =====\n"
    "__version__ = '0.28.1'\n"
    "===== FILE: httpx/_auth.py =====\n"
    "class BasicAuth(Auth): ...\n"
)


def test_arm_broadcast_sends_full_raw_identically() -> None:
    arms = build_arms(SYNTHETIC_BUNDLE, fact_blocks(), as_of=AS_OF)
    systems = [messages[0]["content"] for _, messages in arms["broadcast"]]
    assert len(systems) == 4
    assert all(system == f"{_ARM_TAGS['broadcast']}\n{SYNTHETIC_BUNDLE}" for system in systems)


def test_arm_prefix_shares_leading_bytes_then_diverges() -> None:
    arms = build_arms(SYNTHETIC_BUNDLE, fact_blocks(), as_of=AS_OF)
    systems = [messages[0]["content"] for _, messages in arms["prefix"]]
    shared_head = f"{_ARM_TAGS['prefix']}\n{SYNTHETIC_BUNDLE}\n\n"
    assert all(system.startswith(shared_head) for system in systems)
    # the four role tails are all distinct
    assert len({system[len(shared_head) :] for system in systems}) == 4


def test_arm_compile_packets_differ_by_role_and_carry_the_right_facts() -> None:
    arms = build_arms(SYNTHETIC_BUNDLE, fact_blocks(), as_of=AS_OF)
    systems = [messages[0]["content"] for _, messages in arms["compile"]]
    assert len(set(systems)) == 4  # genuinely divergent per-role packets
    for role, messages in arms["compile"]:
        agent = AGENT_BY_ROLE[role]
        content = messages[0]["content"].lower()
        for required in agent.required:
            assert required.lower() in content
        for forbidden in agent.forbidden:
            assert forbidden.lower() not in content


def test_scorer_requires_all_and_forbids_none() -> None:
    planner = AGENT_BY_ROLE["planner"]
    passed, missing, wrong = score_quality("_client", planner)
    assert passed and missing == [] and wrong == []
    # wrong answer: contains the forbidden fact, misses the required fact
    passed, missing, wrong = score_quality("_models", planner)
    assert not passed and missing == ["_client"] and wrong == ["_models"]

    implementer = AGENT_BY_ROLE["implementer"]
    # a verbose-but-correct answer that mentions the distractor is scored wrong (documented)
    passed, missing, wrong = score_quality(
        "get() and post() call request(), not stream()", implementer
    )
    assert not passed and missing == [] and wrong == ["stream"]

    tester = AGENT_BY_ROLE["tester"]
    assert score_quality("0.28.1", tester)[0]
    passed, missing, wrong = score_quality("0.28.0", tester)
    assert not passed and missing == ["0.28.1"] and wrong == ["0.28.0"]

    reviewer = AGENT_BY_ROLE["reviewer"]
    assert score_quality("BasicAuth", reviewer)[0]
    passed, missing, wrong = score_quality("DigestAuth", reviewer)
    assert not passed and missing == ["basicauth"] and wrong == ["digestauth"]


def test_dry_run_is_deterministic() -> None:
    fixture = {"repo": "synthetic", "commit": "deadbeef", "bundle_tokens_tiktoken": 0, "files": []}
    first = dry_run_payload(SYNTHETIC_BUNDLE, fact_blocks(), fixture=fixture, as_of=AS_OF)
    second = dry_run_payload(SYNTHETIC_BUNDLE, fact_blocks(), fixture=fixture, as_of=AS_OF)
    assert first == second
    # every arm reports 4 roles and a positive token total
    for arm in ("broadcast", "prefix", "compile"):
        assert first["arms"][arm]["prompt_tokens_total"] > 0
        assert len(first["arms"][arm]["roles"]) == 4
