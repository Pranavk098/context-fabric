"""Offline tests for the question-guided skimmer ``kernel/router/skim.py`` (no model, no network).

Asserts determinism, verbatim line + anchor preservation, budget respect, the
import-binding rule, and that the compiler hand-off flag is OFF by default
(byte-identical) and ON switches the packet to the skimmed source.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from kernel.cartridge.serializer import count_tokens
from kernel.router.compiler import compile_packet
from kernel.router.skim import skim
from protocol.canonical import packet_content_hash
from protocol.models import (
    BlockKind,
    ContextBlock,
    Creator,
    CreatorType,
    Fidelity,
    MemoryTier,
    Recipient,
    Scope,
)

SCOPE = Scope(tenant_id="t1", project_id="p1", workflow_id="w1", task_id="task1")
AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
RECIPIENT = Recipient(agent_id="a1", role="implementer", stage="implement")


def _mk(bid: str, content: str, seq: int) -> ContextBlock:
    return ContextBlock(
        block_id=bid,
        block_kind=BlockKind.fact,
        memory_tier=MemoryTier.ledger,
        content=content,
        content_hash="sha256:" + bid,
        token_estimate=count_tokens(content),
        source_event_seq=seq,
        creator=Creator(type=CreatorType.agent, id="a"),
        fidelity=Fidelity.extract,
        trust_domain="t1",
    )


def _write_area_module(root: Path) -> None:
    (root / "m.py").write_text(
        "import math as _m\n"
        "import zlib\n\n"
        "def area(radius):\n"
        "    # circle area\n"
        "    return _m.pi * radius * radius\n",
        encoding="utf-8",
    )


# ---- skim core ------------------------------------------------------------------------


def test_skim_is_deterministic(tmp_path: Path) -> None:
    _write_area_module(tmp_path)
    a = skim("area of a circle from radius", ["m.py"], root=tmp_path, budget=1000)
    b = skim("area of a circle from radius", ["m.py"], root=tmp_path, budget=1000)
    assert a == b


def test_skim_keeps_real_lines_verbatim_with_anchors(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "def f(x):\n    # keep this exact line\n    return x + 1\n", encoding="utf-8"
    )
    out = skim("f add one", ["m.py"], root=tmp_path, budget=1000)
    assert "===== FILE: m.py:1 =====" in out
    assert "===== FILE: m.py:1 =====\ndef f(x):" in out  # anchor immediately before the line
    assert "    # keep this exact line" in out
    assert "    return x + 1" in out


def test_skim_respects_budget(tmp_path: Path) -> None:
    body = "\n\n".join(f"def fn_{i}(x):\n    # helper {i}\n    return x + {i}\n" for i in range(40))
    (tmp_path / "m.py").write_text(body, encoding="utf-8")
    for budget in (1, 10, 50, 200, 1000, 4000):
        out = skim("helper function", ["m.py"], root=tmp_path, budget=budget)
        assert count_tokens(out) <= budget
        if budget <= 1:
            assert out == ""


def test_skim_import_binding_rule(tmp_path: Path) -> None:
    _write_area_module(tmp_path)
    out = skim("area of a circle from radius", ["m.py"], root=tmp_path, budget=1000)
    assert "def area(radius):" in out  # def line always kept
    assert "return _m.pi * radius * radius" in out  # verbatim body
    assert "import math as _m" in out  # binds _m, used in the kept line
    assert "import zlib" not in out  # unrelated import not bound to any kept name


# ---- compiler hand-off flag -----------------------------------------------------------


def test_handoff_flag_off_is_byte_identical() -> None:
    blocks = [
        _mk("f1", "The router is deterministic", 1),
        _mk("d1", "decision: keep the fact", 2),
    ]
    kw = dict(
        scope=SCOPE,
        recipient=RECIPIENT,
        objective="implement deterministic routing",
        stage="implement",
        blocks=blocks,
        as_of=AS_OF,
    )
    default = compile_packet(**kw)
    explicit_off = compile_packet(**kw, handoff_source=None)
    assert default.packet == explicit_off.packet  # byte-identical, flag off


def test_handoff_flag_on_uses_skimmed_source() -> None:
    source = "===== FILE: m.py:1 =====\ndef f():\n    return 1\n"
    r = compile_packet(
        scope=SCOPE,
        recipient=RECIPIENT,
        objective="what does f return",
        stage="implement",
        blocks=[_mk("f1", "irrelevant fabric", 1)],
        as_of=AS_OF,
        budget_base=4000,
        handoff_source=source,
    )
    obs = r.packet.tiers.tier3.observations
    assert [b.block_id for b in obs] == ["blk_handoff_skim"]
    assert "return 1" in (obs[0].content or "")
    assert "irrelevant fabric" not in "\n".join(b.content or "" for b in obs)
    assert r.packet.total_tokens <= r.packet.budget.hard_total_tokens
    assert packet_content_hash(r.packet) == r.packet.hashes.content_hash
    assert r.trace["handoff"] is True
