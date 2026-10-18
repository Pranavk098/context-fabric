"""Exit-gate parity test: the raw compile path and the LangGraph adapter agree, per node.

Drives the *same* logical workflow (planner -> worker -> reviewer over one fabric) two
ways — (a) ``compile_packet`` directly, (b) through the LangGraph graph in
``adapters/langgraph/example.py`` — and asserts byte-identical packet hashes and identical
policy traces for every node. Skips cleanly when the optional ``langgraph`` extra is not
installed (CI installs ``--extra dev`` only, so this test self-skips there).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("langgraph")

from adapters.langgraph.example import (  # noqa: E402
    OBJECTIVE,
    PLANNER,
    REVIEWER,
    SCOPE,
    WORKER,
    build_graph,
    demo_blocks,
)
from adapters.langgraph.intercept import node_model_input  # noqa: E402
from kernel.router.compiler import compile_packet  # noqa: E402
from protocol.models import Recipient  # noqa: E402
from sidecar.render import render_packet  # noqa: E402

AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
_NODES: tuple[tuple[str, Recipient], ...] = (
    ("planner", PLANNER),
    ("worker", WORKER),
    ("reviewer", REVIEWER),
)


def _raw(recipient: Recipient):
    return compile_packet(
        scope=SCOPE,
        recipient=recipient,
        objective=OBJECTIVE,
        stage=recipient.stage,
        blocks=demo_blocks(),
        as_of=AS_OF,
    )


def test_raw_and_langgraph_are_byte_identical_per_node() -> None:
    graph, results = build_graph(as_of=AS_OF)
    graph.invoke({"objective": OBJECTIVE, "blocks": demo_blocks()})
    adapter = dict(results)
    assert sorted(adapter) == ["planner", "reviewer", "worker"]  # all 3 nodes ran once
    for name, recipient in _NODES:
        raw = _raw(recipient)
        got = adapter[name]
        assert got.packet.hashes.content_hash == raw.packet.hashes.content_hash
        assert got.packet == raw.packet  # full packet, not just the hash
        assert got.trace == raw.trace  # identical policy trace
        # and the node's model input is exactly the raw render
        assert node_model_input(got)["content"] == render_packet(raw.packet)


def test_roles_diverge_but_parity_holds_per_role() -> None:
    """The workflow exercises divergent roles (reviewer is denied obs1), yet each role's
    packet/trace is still exactly what the raw path produces for that role."""
    planner = _raw(PLANNER)
    reviewer = _raw(REVIEWER)
    assert planner.trace != reviewer.trace
    assert any(
        e["id"] == "obs1" and e["reason"] == "role_denied" for e in reviewer.trace["excluded"]
    )
    assert "obs1" in planner.trace["included"]
    assert "obs1" not in [b.block_id for b in reviewer.packet.tiers.tier1.blocks]
