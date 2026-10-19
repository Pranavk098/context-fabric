"""Offline tests for the pre-registered B4 v3 harness (no model, no network).

Asserts the token-level scorer (raw_host vs host, MD5 vs MD5-SESS, read vs aread,
request vs RequestError, and the forbidden-in-objective ignore rule), the objectives
sha256 guard, the exact-import mechanical fabric + ownership divergence, the AST-chunk
retrieval baseline, and that the dry-run is deterministic and reports a per-objective
recall diagnostic. See ``docs/engineering/B4-V3-PREREGISTRATION.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import evals.b4_v3 as b4v3
from evals.b4_v3 import (
    AgentSpec,
    build_arms,
    build_ast_chunks,
    build_compile_blocks,
    dry_run_payload,
    load_objectives,
    score_quality,
    score_quality_v2,
)
from evals.mech_fabric import build_blocks
from protocol.models import BlockKind

AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
MODEL = "Qwen/Qwen2.5-1.5B-Instruct"


# ---- scorer (token-level, hand-written answers) ---------------------------------------


def test_scorer_raw_host_vs_host() -> None:
    objective = "Which property returns the IDNA bytes?"
    ok, missing, wrong = score_quality("raw_host", objective, ("raw_host",), ("host",))
    assert ok and not missing and not wrong  # "host" is not a token of "raw_host"

    ok, missing, wrong = score_quality("host", objective, ("raw_host",), ("host",))
    assert not ok and missing == ["raw_host"] and wrong == ["host"]


def test_scorer_md5_vs_md5_sess() -> None:
    objective = "Which algorithm names map to md5?"
    ok, missing, wrong = score_quality("MD5, MD5-SESS", objective, ("MD5", "MD5-SESS"), ("SHA",))
    assert ok and not missing and not wrong

    ok, missing, wrong = score_quality("MD5-SESS", objective, ("MD5", "MD5-SESS"), ("SHA",))
    # "MD5-SESS" is one token (hyphen kept); it must not satisfy required "MD5"
    assert not ok and missing == ["MD5"] and not wrong


def test_scorer_read_vs_aread() -> None:
    objective = "Which methods read the whole body?"
    ok, missing, wrong = score_quality("read, aread", objective, ("read", "aread"), ("content",))
    assert ok and not missing and not wrong

    ok, missing, wrong = score_quality("aread", objective, ("read", "aread"), ("content",))
    assert not ok and missing == ["read"] and not wrong


def test_scorer_request_vs_request_error() -> None:
    objective = "Which is the base error and which helper?"
    ok, missing, wrong = score_quality(
        "RequestError, request", objective, ("RequestError", "request"), ("HTTPError",)
    )
    assert ok and not missing and not wrong

    ok, missing, wrong = score_quality(
        "RequestError", objective, ("RequestError", "request"), ("HTTPError",)
    )
    assert not ok and missing == ["request"] and not wrong


def test_scorer_forbidden_in_objective_is_ignored() -> None:
    objective = (
        "Which property returns the URL-decoded path, and which returns the raw bytes of "
        "the path plus query?"
    )
    # a restated-question answer echoes "query" (forbidden) but "query" is in the objective
    ok, missing, wrong = score_quality(
        "The path and raw_path properties return the path plus query",
        objective,
        ("path", "raw_path"),
        ("query", "netloc", "host"),
    )
    assert ok and not missing and not wrong

    # without the ignore rule (objective no longer mentions "query") it is a forbidden hit
    ok, _missing, wrong = score_quality(
        "The path and raw_path properties return the path plus query",
        "Which property returns the decoded path and the raw bytes?",
        ("path", "raw_path"),
        ("query",),
    )
    assert not ok and wrong == ["query"]


# ---- scorer v2 (explicit flag; v1 stays the frozen default) ----------------------------


def test_scorer_v2_strips_trailing_dot() -> None:
    objective = "Which methods read the whole body?"
    # v1 keeps the dot: "aread." is not the required token "aread"
    ok, missing, _ = score_quality("aread.", objective, ("aread",), ("content",))
    assert not ok and missing == ["aread"]
    # v2 strips the trailing dot, so "aread." satisfies "aread"
    ok, missing, _ = score_quality_v2("aread.", objective, ("aread",), ("content",))
    assert ok and not missing


def test_scorer_v2_strips_leading_slash_and_trailing_hyphen() -> None:
    objective = "Which methods read the whole body?"
    ok, missing, _ = score_quality_v2("/read-", objective, ("read",), ("content",))
    assert ok and not missing


def test_scorer_v2_preserves_inner_punct() -> None:
    objective = "Which algorithm names map to md5?"
    ok, missing, _ = score_quality_v2("MD5-SESS", objective, ("MD5-SESS",), ("SHA",))
    assert ok and not missing  # inner hyphen survives; nothing to strip at the edges
    ok, missing, _ = score_quality_v2("_client.get", objective, ("_client.get",), ("post",))
    assert ok and not missing  # inner dot survives stripping


def test_scorer_v2_drops_punct_only_tokens() -> None:
    objective = "Which methods read the whole body?"
    ok, missing, _ = score_quality_v2("--- read ...", objective, ("read",), ("content",))
    assert ok and not missing  # "---" and "..." strip to empty and are dropped


# ---- objectives sha256 guard ----------------------------------------------------------


def test_objectives_sha256_matches_documented_crlf_normalized() -> None:
    data = b4v3.OBJECTIVES_FILE.read_bytes()
    assert b4v3._objectives_sha256(data) == b4v3.OBJECTIVES_SHA256


def test_load_objectives_returns_20_and_aborts_on_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    objectives = load_objectives()
    assert len(objectives) == 20
    assert {o.role for o in objectives} == {"planner", "implementer", "tester", "reviewer"}
    assert all(o.required for o in objectives)
    assert all(o.forbidden for o in objectives)

    monkeypatch.setattr(b4v3, "OBJECTIVES_SHA256", "0" * 64)
    with pytest.raises(SystemExit):
        load_objectives()


# ---- fabric / arms --------------------------------------------------------------------


def _synthetic_httpx(root: Path) -> None:
    pkg = root / "httpx"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        '"""httpx package."""\nfrom ._api import get\n', encoding="utf-8"
    )
    (pkg / "_config.py").write_text(
        "DEFAULT_MAX_REDIRECTS = 20\nDEFAULT_TIMEOUT_CONFIG = Timeout(timeout=5.0)\n",
        encoding="utf-8",
    )
    (pkg / "_auth.py").write_text(
        "import hashlib\n\n"
        'class DigestAuth:\n    """Digest auth."""\n'
        "    def __init__(self):\n        self.map = {'MD5': None}\n",
        encoding="utf-8",
    )
    (pkg / "_api.py").write_text(
        'def request(method, url):\n    """Send a request."""\n    return None\n',
        encoding="utf-8",
    )


_SYNTH_OBJECTIVES = (
    AgentSpec("p1", "planner", "What is the default max redirects?", ("20",), ("5.0",)),
    AgentSpec(
        "i1",
        "implementer",
        "Which auth class parses a digest challenge?",
        ("DigestAuth",),
        ("BasicAuth",),
    ),
    AgentSpec("t1", "tester", "Which helper sends a request?", ("request",), ("get",)),
)


def test_build_blocks_import_statements_flag_keeps_default_unchanged(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "from ._config import DEFAULT_TIMEOUT_CONFIG\nimport os\n", encoding="utf-8"
    )
    default = "\n".join(b.content or "" for b in build_blocks(tmp_path, ["m.py"]))
    exact = "\n".join(
        b.content or "" for b in build_blocks(tmp_path, ["m.py"], import_statements=True)
    )
    assert "from ._config import DEFAULT_TIMEOUT_CONFIG" not in default  # roots, unchanged
    assert "from ._config import DEFAULT_TIMEOUT_CONFIG" in exact


def test_build_compile_blocks_applies_ownership_and_drops_artifact(tmp_path: Path) -> None:
    _synthetic_httpx(tmp_path)
    ownership = {"planner": ("httpx/_config.py",), "implementer": ("httpx/_auth.py",)}
    blocks = build_compile_blocks(tmp_path, ["httpx/_config.py", "httpx/_auth.py"], ownership)
    assert blocks  # non-empty
    assert all(b.block_kind is not BlockKind.artifact for b in blocks)  # cartridge dropped
    text = "\n".join(b.content or "" for b in blocks)
    assert "DEFAULT_MAX_REDIRECTS = 20" in text  # assignment fact
    by_file: dict[str, set[str]] = {}
    for b in blocks:
        assert b.ref_uri is not None
        by_file.setdefault(b.ref_uri, set()).update(b.denied_roles)
    # _config.py is owned by planner -> denied to implementer; _auth.py owned by implementer
    # -> denied to planner
    assert by_file["httpx/_config.py"] == {"implementer"}
    assert by_file["httpx/_auth.py"] == {"planner"}


def test_build_ast_chunks_produces_headers_and_symbols(tmp_path: Path) -> None:
    _synthetic_httpx(tmp_path)
    chunks = build_ast_chunks(tmp_path, ["httpx/_auth.py"])
    assert chunks
    kinds = {(b.ref_uri, b.symbols_exported) for b in chunks}
    assert ("httpx/_auth.py", ()) in kinds  # module header chunk
    assert ("httpx/_auth.py", ("DigestAuth",)) in kinds  # class chunk
    assert all(b.content and b.content.startswith("===== FILE:") for b in chunks)


def test_arms_built_correctly(tmp_path: Path) -> None:
    _synthetic_httpx(tmp_path)
    bundle = build_arms(tmp_path, _SYNTH_OBJECTIVES, model=MODEL, as_of=AS_OF)
    assert set(bundle.arms) == {"truncate", "retrieval", "compile"}
    for pairs in bundle.arms.values():
        assert len(pairs) == len(_SYNTH_OBJECTIVES)

    # truncate: tag + role line + a prefix of the rendered package, and it fits the window
    t_agent, t_messages = bundle.arms["truncate"][0]
    t_system = t_messages[0]["content"]
    assert t_system.startswith("B4v3::truncate::")
    assert "ROLE: planner" in t_system
    assert (
        bundle.counter.count(t_system) + bundle.counter.count(t_agent.objective)
        < b4v3.MAX_MODEL_LEN
    )

    # retrieval: tag + BM25-selected chunks (the objectives overlap the synthetic source)
    r_agent, r_messages = bundle.arms["retrieval"][1]  # i1: "digest challenge" -> DigestAuth
    assert r_messages[0]["content"].startswith("B4v3::retrieval::")
    assert "===== FILE:" in r_messages[0]["content"]

    # compile: tag + rendered packet carrying the role instruction and the terse contract
    c_agent, c_messages = bundle.arms["compile"][0]
    c_system = c_messages[0]["content"]
    assert c_system.startswith("B4v3::compile::")
    assert "ROLE: planner" in c_system
    assert "ONLY the comma-separated answer values" in c_system


def test_dry_run_is_deterministic_and_reports_recall(tmp_path: Path) -> None:
    _synthetic_httpx(tmp_path)
    first = dry_run_payload(tmp_path, _SYNTH_OBJECTIVES, model=MODEL, as_of=AS_OF)
    second = dry_run_payload(tmp_path, _SYNTH_OBJECTIVES, model=MODEL, as_of=AS_OF)
    assert first == second
    assert first["tokenizer"] in {"qwen", "tiktoken"}
    for arm in ("truncate", "retrieval", "compile"):
        assert first["arms"][arm]["prompt_tokens_total"] > 0
    # recall diagnostic covers retrieval and compile (not truncate), per objective
    assert {row["arm"] for row in first["recall"]} == {"retrieval", "compile"}
    assert {row["objective_id"] for row in first["recall"]} == {
        o.objective_id for o in _SYNTH_OBJECTIVES
    }
    for row in first["recall"]:
        assert set(row["facts_in_packet"]) == set(row["required"])
        assert isinstance(row["all_in_packet"], bool)
