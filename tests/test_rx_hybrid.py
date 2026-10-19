"""Offline tests for the option-A probe ``evals/rx_hybrid.py`` (no model, no network).

Asserts the fixed scorer (trailing-punctuation strip), the retrieval/dedupe helpers, that
the R/H1/H2 arms build correctly on a synthetic fixture, and that the dry-run is
deterministic and reports a per-arm recall diagnostic. See
``docs/engineering/RESEARCH-ROUND-C-INTERNAL.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import evals.rx_hybrid as rx
from evals.b4_v3 import AgentSpec, build_ast_chunks, package_files
from evals.mech_fabric import build_project_cartridge
from evals.rx_hybrid import (
    _Counter,
    _dedupe,
    _tokens_fixed,
    build_arm_messages,
    dry_run_payload,
    score_quality_fixed,
)
from protocol.models import BlockKind, ContextBlock

AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
MODEL = "Qwen/Qwen2.5-1.5B-Instruct"


# ---- fixed scorer --------------------------------------------------------------------


def test_fixed_tokens_strips_leading_trailing_punctuation() -> None:
    assert _tokens_fixed("...is 5.0.") == ["is", "5.0"]
    assert _tokens_fixed("-foo /bar/") == ["foo", "bar"]
    assert _tokens_fixed("raw_host") == ["raw_host"]  # no strip of '_' or internal chars
    assert _tokens_fixed("application/octet-stream") == ["application/octet-stream"]


def test_fixed_scorer_matches_5_dot_0() -> None:
    objective = "What is the default keepalive_expiry?"
    # frozen: "5.0." tokenizes to "5.0." which misses "5.0"
    ok_frozen, _m, _w = rx.score_frozen("5.0.", objective, ("5.0",), ("20",))
    assert not ok_frozen
    ok_fixed, missing, wrong = score_quality_fixed("5.0.", objective, ("5.0",), ("20",))
    assert ok_fixed and not missing and not wrong


def test_fixed_scorer_raw_host_vs_host() -> None:
    objective = "Which property returns the IDNA bytes?"
    ok, missing, wrong = score_quality_fixed("raw_host", objective, ("raw_host",), ("host",))
    assert ok and not missing and not wrong


# ---- retrieval / dedupe --------------------------------------------------------------


def _mk_chunk(bid: str, content: str) -> ContextBlock:
    from protocol.models import Creator, CreatorType, Fidelity, MemoryTier

    return ContextBlock(
        block_id=bid,
        block_kind=BlockKind.fact,
        memory_tier=MemoryTier.ledger,
        content=content,
        content_hash="sha256:" + content,
        token_estimate=len(content.split()),
        source_event_seq=1,
        creator=Creator(type=CreatorType.tool, id="t"),
        fidelity=Fidelity.extract,
        trust_domain="t",
    )


def test_dedupe_drops_duplicates_and_contained() -> None:
    a = _mk_chunk("a", "def foo(): return 1")
    b = _mk_chunk("b", "def foo(): return 1")  # exact dup
    c = _mk_chunk("c", "def foo(): return 1  # x")  # contains a
    assert [x.block_id for x in _dedupe([a, b, c, _mk_chunk("d", "distinct")])] == ["a", "d"]


# ---- arms / dry-run ------------------------------------------------------------------


def _synthetic_httpx(root: Path) -> None:
    pkg = root / "httpx"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        '"""httpx package."""\nfrom ._api import get\n', encoding="utf-8"
    )
    (pkg / "_config.py").write_text(
        "def get_max_redirects() -> int:\n"
        '    """Return the default max redirects."""\n'
        "    return 20\n",
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
)


def test_arms_build_r_h1_h2(tmp_path: Path) -> None:
    _synthetic_httpx(tmp_path)
    files = package_files(tmp_path)
    chunks = build_ast_chunks(tmp_path, files)
    cartridge = build_project_cartridge(tmp_path, files)
    counter = _Counter(MODEL)

    for arm in ("R", "H1", "H2"):
        pairs = build_arm_messages(
            arm,
            2000,
            _SYNTH_OBJECTIVES,
            fixture=tmp_path,
            files=files,
            chunks=chunks,
            cartridge=cartridge,
            counter=counter,
            as_of=AS_OF,
        )
        assert len(pairs) == len(_SYNTH_OBJECTIVES)
        system = pairs[0][1][0]["content"]
        assert system.startswith(f"Rx::{arm}::2000::")

    r_system = build_arm_messages(
        "R",
        2000,
        _SYNTH_OBJECTIVES,
        fixture=tmp_path,
        files=files,
        chunks=chunks,
        cartridge=cartridge,
        counter=counter,
        as_of=AS_OF,
    )[0][1][0]["content"]
    assert "===== FILE:" in r_system  # raw chunks

    h1_system = build_arm_messages(
        "H1",
        2000,
        _SYNTH_OBJECTIVES,
        fixture=tmp_path,
        files=files,
        chunks=chunks,
        cartridge=cartridge,
        counter=counter,
        as_of=AS_OF,
    )[0][1][0]["content"]
    assert "ONLY the comma-separated answer values" in h1_system  # terse contract
    assert "OBJECTIVE:" in h1_system  # compiler instruction

    h2_system = build_arm_messages(
        "H2",
        2000,
        _SYNTH_OBJECTIVES,
        fixture=tmp_path,
        files=files,
        chunks=chunks,
        cartridge=cartridge,
        counter=counter,
        as_of=AS_OF,
    )[0][1][0]["content"]
    assert "Project Hologram" in h2_system  # repo-map header (cartridge)
    assert "OBJECTIVE:" not in h2_system  # no compiler

    # import lines: the DigestAuth objective retrieves _auth.py, which imports hashlib
    h2_auth = build_arm_messages(
        "H2",
        2000,
        _SYNTH_OBJECTIVES,
        fixture=tmp_path,
        files=files,
        chunks=chunks,
        cartridge=cartridge,
        counter=counter,
        as_of=AS_OF,
    )[1][1][0]["content"]
    assert "import hashlib" in h2_auth


def test_dry_run_deterministic_and_reports_recall(tmp_path: Path) -> None:
    _synthetic_httpx(tmp_path)
    first = dry_run_payload(tmp_path, _SYNTH_OBJECTIVES, model=MODEL, as_of=AS_OF)
    second = dry_run_payload(tmp_path, _SYNTH_OBJECTIVES, model=MODEL, as_of=AS_OF)
    assert first == second
    assert set(first["arms"]) == {
        f"{arm}:{budget}" for budget in (1000, 2000, 4000) for arm in ("R", "H1", "H2")
    }
    for summary in first["arms"].values():
        assert summary["prompt_tokens_total"] > 0
    recall = first["recall"]
    assert recall
    for row in recall:
        assert row["arm"].startswith(("R:", "H1:", "H2:"))
        assert set(row["facts_in_packet"]) == set(row["required"])
        assert isinstance(row["all_in_packet"], bool)
