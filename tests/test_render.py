"""Renderer tests — order: shared Tier 0 head, instruction, prohibitions/contract/recap tail."""

from __future__ import annotations

import pytest

from kernel.cartridge.serializer import Cartridge, count_tokens
from kernel.router import compile_packet
from protocol.canonical import sha256_hex
from protocol.models import (
    BlockKind,
    ContextBlock,
    ContextPacket,
    Creator,
    CreatorType,
    Fidelity,
    Hashes,
    MemoryTier,
    RecencyAnchor,
    Recipient,
    Scope,
    Tier0Cartridge,
    Tier1Ledger,
    Tier3Register,
    Tiers,
    TokenBudget,
)
from sidecar.render import render_packet

_HEADER = "PROHIBITIONS (approaches that failed \u2014 do not repeat):"
_RECAP_HEADER = "KEY CONSTRAINTS (restated):"


def _block(block_id: str, kind: BlockKind, content: str, seq: int = 1) -> ContextBlock:
    return ContextBlock(
        block_id=block_id,
        block_kind=kind,
        memory_tier=MemoryTier.ledger,
        content=content,
        content_hash="sha256:" + block_id,
        token_estimate=1,
        source_event_seq=seq,
        creator=Creator(type=CreatorType.agent, id="tester"),
        fidelity=Fidelity.extract,
        trust_domain="default",
    )


def _packet(
    anchor: RecencyAnchor | None,
    tier1: tuple[ContextBlock, ...] = (_block("d1", BlockKind.decision, "Use Postgres", seq=1),),
    tier0_markdown: str = "",
) -> ContextPacket:
    return ContextPacket(
        packet_id="cpk_t",
        route_id="rte_t",
        watermark_event_seq=1,
        scope=Scope(tenant_id="t", project_id="p", workflow_id="w", task_id="k"),
        recipient=Recipient(agent_id="a", role="user", stage="chat"),
        budget=TokenBudget(),
        tiers=Tiers(
            tier0=Tier0Cartridge(
                cartridge_id="c", kv_prefix_hash="sha256:x", content_markdown=tier0_markdown
            ),
            tier1=Tier1Ledger(blocks=tier1),
            tier3=Tier3Register(
                instruction=_block("blk_i", BlockKind.instruction, "OBJECTIVE: test"),
                recency_anchor=anchor,
            ),
        ),
        total_tokens=10,
        hashes=Hashes(content_hash="sha256:y"),
    )


def test_tier0_renders_before_instruction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CF_RECAP", "0")
    markdown = "# Project Hologram: p @ deadbeef\n- core.py: shared()"
    lines = render_packet(
        _packet(RecencyAnchor(output_contract="Contract."), tier0_markdown=markdown)
    ).splitlines()
    assert lines[:3] == markdown.splitlines() + ["OBJECTIVE: test"]


def test_empty_tier0_render_is_byte_identical_to_old_instruction_first_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CF_RECAP", "0")
    assert render_packet(_packet(RecencyAnchor(output_contract="Contract."))) == (
        "OBJECTIVE: test\n[decision] Use Postgres\nContract."
    )


def test_shared_tier0_prefix_identical_across_agents_and_objectives() -> None:
    """Two agents, one store: the first len(tier0 markdown) bytes are byte-identical."""
    markdown = "# Project Hologram: p @ deadbeef\n- core.py: shared()"
    cartridge = Cartridge(
        cartridge_id="cfx_p_deadbeef",
        tenant_id="t",
        project_id="p",
        commit_hash="deadbeef00",
        hologram_markdown=markdown,
        token_count=count_tokens(markdown),
        kv_prefix_hash="sha256:" + sha256_hex(markdown.encode("utf-8")),
    )
    blocks = [_block("d1", BlockKind.decision, "Use Postgres", seq=1)]
    renders = [
        render_packet(
            compile_packet(
                scope=Scope(tenant_id="t", project_id="p", workflow_id="w", task_id="k"),
                recipient=Recipient(agent_id=agent, role=role, stage="chat"),
                objective=objective,
                stage="chat",
                blocks=blocks,
                cartridge=cartridge,
                watermark_event_seq=1,
            ).packet
        )
        for agent, role, objective in (
            ("a1", "implementer", "OBJECTIVE_ALPHA_SENTINEL: pick A"),
            ("a2", "reviewer", "OBJECTIVE_BETA_SENTINEL: pick B"),
        )
    ]
    prefix_len = len(markdown.encode("utf-8"))
    first, second = (text.encode("utf-8") for text in renders)
    assert first[:prefix_len] == second[:prefix_len] == markdown.encode("utf-8")
    assert b"SENTINEL" not in first[:prefix_len]  # no objective text inside the shared prefix
    assert first[prefix_len:] != second[prefix_len:]  # divergence starts after the shared block
    assert first != second


def test_tail_order_is_prohibitions_contract_then_recap_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CF_RECAP", "1")
    anchor = RecencyAnchor(
        output_contract="Reply with the required structured output only.",
        prohibitions=("Dead end: do NOT scale ingestion with mirror-poller", "Second dead end"),
    )
    lines = render_packet(_packet(anchor)).splitlines()
    assert lines[0] == "OBJECTIVE: test"
    assert lines[1] == "[decision] Use Postgres"
    header_index = lines.index(_HEADER)
    contract_index = lines.index(anchor.output_contract)
    assert contract_index == header_index + 3  # header + two prohibition lines
    assert lines[contract_index + 1] == _RECAP_HEADER
    assert lines[-1] == "- [decision] Use Postgres"  # recap is the last section


def test_empty_prohibitions_skip_header_but_keep_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CF_RECAP", "0")
    text = render_packet(_packet(RecencyAnchor(output_contract="Contract.")))
    assert _HEADER not in text
    assert text.endswith("Contract.")


def test_none_anchor_renders_without_anchor_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CF_RECAP", "0")
    text = render_packet(_packet(None))
    assert text.splitlines()[0] == "OBJECTIVE: test"
    assert _HEADER not in text
    assert "required structured output" not in text
    assert text.endswith("Use Postgres")


def test_none_anchor_still_renders_recap_last(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CF_RECAP", "1")
    lines = render_packet(_packet(None)).splitlines()
    assert _HEADER not in lines
    assert lines[-2] == _RECAP_HEADER
    assert lines[-1] == "- [decision] Use Postgres"


def test_recap_capped_at_two_most_recent_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CF_RECAP", "1")
    tier1 = (
        _block("d1", BlockKind.decision, "oldest constraint", seq=1),
        _block("d2", BlockKind.decision, "middle constraint", seq=2),
        _block("d3", BlockKind.decision, "newest constraint", seq=3),
    )
    lines = render_packet(_packet(RecencyAnchor(output_contract="Contract."), tier1)).splitlines()
    assert lines[-3:] == [
        _RECAP_HEADER,
        "- [decision] newest constraint",
        "- [decision] middle constraint",
    ]
    assert "oldest constraint" not in "\n".join(lines[-3:])


def test_recap_skips_blocks_already_covered_by_prohibitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CF_RECAP", "1")
    prohibition = "Dead end: do not scale ingestion with mirror-poller"
    tier1 = (
        _block("d1", BlockKind.decision, "Use Postgres", seq=1),
        _block("nk1", BlockKind.negative_knowledge, prohibition, seq=2),
    )
    lines = render_packet(
        _packet(RecencyAnchor(output_contract="Contract.", prohibitions=(prohibition,)), tier1)
    ).splitlines()
    assert lines[-2:] == [_RECAP_HEADER, "- [decision] Use Postgres"]
    assert lines.index("- " + prohibition) < lines.index(_RECAP_HEADER)


def test_recap_skips_long_block_whose_prohibition_prefix_is_rendered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CF_RECAP", "1")
    content = " ".join(f"w{i}" for i in range(30))
    prohibition = " ".join(content.split()[:12])
    tier1 = (_block("nk1", BlockKind.negative_knowledge, content, seq=1),)
    lines = render_packet(
        _packet(RecencyAnchor(output_contract="Contract.", prohibitions=(prohibition,)), tier1)
    ).splitlines()
    assert _RECAP_HEADER not in lines
    assert lines.count("- " + prohibition) == 1
    assert lines[-1] == "Contract."


def test_recap_truncates_long_blocks_to_64_words(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CF_RECAP", "1")
    content = " ".join(f"w{i}" for i in range(70))
    tier1 = (_block("d1", BlockKind.decision, content, seq=1),)
    lines = render_packet(_packet(RecencyAnchor(output_contract="C."), tier1)).splitlines()
    recap_line = lines[-1]
    assert recap_line.startswith("- [decision] w0 ")
    assert recap_line.endswith("w63\u2026")
    assert "w64" not in recap_line


def test_recap_disabled_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CF_RECAP", "0")
    anchor = RecencyAnchor(
        output_contract="Contract.",
        prohibitions=("Dead end: do NOT scale ingestion",),
    )
    text = render_packet(_packet(anchor))
    assert _RECAP_HEADER not in text
    assert text.endswith("Contract.")
