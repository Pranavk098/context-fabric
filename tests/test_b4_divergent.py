"""Offline tests for the pre-registered B4 v2 divergent-role harness (no model, no network).

Asserts the mechanical ingester reads only the files it is given (never objectives or
answer keys), that the fabric is deterministic, that role divergence comes only from the
file-ownership map, the required+forbidden scorer, the raw-answerability construction
check, and that the dry-run payload is byte-deterministic and reports a per-objective
recall diagnostic. See ``docs/engineering/B4-PREREGISTRATION-v2.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from evals.b4_divergent import (
    _AGENTS,
    AGENT_BY_ID,
    OWNERSHIP,
    build_mech_blocks,
    dry_run_payload,
    score_quality,
    verify_raw_answerable,
)
from evals.mech_fabric import build_blocks
from kernel.router.policy_engine import apply_policy

AS_OF = datetime(2026, 1, 1, tzinfo=UTC)


def _synthetic_httpx(root: Path) -> None:
    """Four minimal httpx files so ``build_mech_blocks`` runs offline."""
    pkg = root / "httpx"
    pkg.mkdir()
    (pkg / "_api.py").write_text(
        "from ._client import Client\n"
        "from ._models import Response\n"
        "from ._config import DEFAULT_TIMEOUT_CONFIG\n"
        "from ._urls import URL\n"
        "\n"
        "def request(method, url):\n"
        '    """Sends an HTTP request."""\n'
        "    return None\n"
        "\n"
        "def stream(method, url):\n"
        '    """Alternative to request that streams."""\n'
        "    yield None\n",
        encoding="utf-8",
    )
    (pkg / "_auth.py").write_text(
        "class Auth:\n"
        '    """Base auth."""\n'
        "    requires_request_body = False\n"
        "    requires_response_body = False\n"
        "\n"
        "class FunctionAuth(Auth):\n"
        "    pass\n"
        "\n"
        "class NetRCAuth(Auth):\n"
        "    pass\n"
        "\n"
        "class DigestAuth(Auth):\n"
        "    pass\n",
        encoding="utf-8",
    )
    (pkg / "_status_codes.py").write_text(
        "class codes:\n"
        '    IM_A_TEAPOT = 418, "I\'m a teapot"\n'
        '    TOO_MANY_REQUESTS = 429, "Too Many Requests"\n'
        '    NOT_FOUND = 404, "Not Found"\n'
        '    SERVICE_UNAVAILABLE = 503, "Service Unavailable"\n'
        "    def is_error(self):\n"
        "        return True\n",
        encoding="utf-8",
    )
    (pkg / "_types.py").write_text(
        "URLTypes = str\n"
        "HeaderTypes = dict\n"
        "RequestContent = bytes\n"
        "ResponseContent = bytes\n"
        "\n"
        "class SyncByteStream:\n"
        "    def __iter__(self):\n"
        "        return iter(())\n",
        encoding="utf-8",
    )


def test_ingester_input_excludes_objectives_and_keys(tmp_path: Path) -> None:
    """The ingester reads only the files it is given — no objective or answer key enters."""
    (tmp_path / "src.py").write_text("def f():\n    return 42\n", encoding="utf-8")
    (tmp_path / "objectives.py").write_text(
        "OBJECTIVE = 'what is the secret answer'\nKEY = 'secret-42'\n", encoding="utf-8"
    )
    blocks = build_blocks(tmp_path, ["src.py"])
    all_text = "\n".join(b.content or "" for b in blocks)
    assert "what is the secret answer" not in all_text
    assert "secret-42" not in all_text
    # every block is either derived from the listed file or the project-wide cartridge
    assert {b.ref_uri for b in blocks} <= {"src.py", None}


def test_ingester_is_deterministic_and_mechanical(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "from os import path\n"
        "VERSION = '1.2.3'\n"
        "\n"
        "def greet(name):\n"
        '    """Return a greeting."""\n'
        "    return name\n",
        encoding="utf-8",
    )
    first = build_blocks(tmp_path, ["m.py"])
    second = build_blocks(tmp_path, ["m.py"])
    assert first == second
    text = "\n".join(b.content or "" for b in first)
    assert "def greet(name)" in text  # signature
    assert "Return a greeting." in text  # docstring first line
    assert "VERSION = '1.2.3'" in text  # assignment


def test_ownership_map_is_the_only_divergence(tmp_path: Path) -> None:
    _synthetic_httpx(tmp_path)
    blocks = build_mech_blocks(tmp_path)
    roles = tuple(OWNERSHIP)
    for block in blocks:
        if block.ref_uri is None:  # project-wide cartridge: shared
            assert block.denied_roles == ()
            continue
        owner = next(role for role, rels in OWNERSHIP.items() if block.ref_uri in rels)
        assert block.denied_roles == tuple(r for r in roles if r != owner)
    # the real policy engine lets each role see only its own file's blocks + the cartridge
    for role in roles:
        allowed, _ = apply_policy(blocks, role=role, trust_domain="default", as_of=AS_OF)
        owned = OWNERSHIP[role][0]
        assert all(b.ref_uri in (owned, None) for b in allowed)
        assert any(b.ref_uri == owned for b in allowed)


def test_scorer_requires_all_and_forbids_none() -> None:
    t1 = AGENT_BY_ID["tester-1"]
    assert score_quality("418, 429", t1)[0]
    passed, missing, wrong = score_quality("451", t1)
    assert not passed and missing == ["418", "429"] and wrong == ["451"]

    i1 = AGENT_BY_ID["implementer-1"]
    assert score_quality("requires_request_body, requires_response_body", i1)[0]
    passed, missing, wrong = score_quality("auth_flow", i1)
    assert not passed and missing == ["requires_request_body", "requires_response_body"]
    assert wrong == ["auth_flow"]

    r3 = AGENT_BY_ID["reviewer-3"]
    assert score_quality("SyncByteStream", r3)[0]
    assert not score_quality("AsyncByteStream", r3)[0]


def test_verify_raw_answerable_only_checks_the_raw_bundle() -> None:
    all_facts = "\n".join(fact for agent in _AGENTS for fact in agent.required)
    verify_raw_answerable(all_facts)  # must not raise
    missing = "\n".join(fact for fact in _AGENTS[0].required[1:])
    with pytest.raises(ValueError, match="missing from raw bundle"):
        verify_raw_answerable(missing)


def test_dry_run_is_deterministic_and_reports_recall(tmp_path: Path) -> None:
    _synthetic_httpx(tmp_path)
    blocks = build_mech_blocks(tmp_path)
    # the raw bundle must carry every required fact (the only construction check)
    raw = "\n".join(fact for agent in _AGENTS for fact in agent.required)
    fixture = {
        "repo": "synthetic",
        "commit": "deadbeef",
        "files": [],
        "bundle_tokens_tiktoken": 0,
    }
    first = dry_run_payload(raw, blocks, fixture=fixture, as_of=AS_OF)
    second = dry_run_payload(raw, blocks, fixture=fixture, as_of=AS_OF)
    assert first == second
    assert first["recall"]  # per-objective recall diagnostic is present
    assert {row["objective_id"] for row in first["recall"]} == set(AGENT_BY_ID)
    for arm in ("broadcast", "prefix", "compile"):
        assert first["arms"][arm]["prompt_tokens_total"] > 0
        assert len(first["arms"][arm]["objectives"]) == len(_AGENTS)
