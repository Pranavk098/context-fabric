"""The reusable LangGraph node interceptor (no LangGraph import needed here).

A LangGraph node invocation is just a ``state`` dict. For a node, this module builds the
compile input the sidecar builds — objective plus fabric blocks — calls the exact same
``kernel.router.compiler.compile_packet`` path, and renders the packet as the node's model
input. It contains no routing, scoring, or policy logic of its own: the compiler and
renderer stay the single source of truth, so a workflow driven through this adapter
compiles byte-identically to the raw API for the same inputs (asserted in
``tests/test_langgraph_parity.py``).

The only two primitives B4 and beyond need:

* ``compile_node_packet(state, *, scope, recipient, ...)`` — state -> ``CompileResult``
  (packet + policy trace), via the same ``compile_packet`` the sidecar calls.
* ``node_model_input(result)`` — the rendered packet as an OpenAI-shaped system message,
  i.e. what the node passes to its chat model.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from kernel.router.compiler import DEFAULT_AS_OF, CompileResult, compile_packet
from protocol.models import Recipient, Scope
from protocol.signing import Signer
from sidecar.render import render_packet

__all__ = ["compile_node_packet", "node_model_input"]


def compile_node_packet(
    state: Mapping[str, Any],
    *,
    scope: Scope,
    recipient: Recipient,
    watermark_event_seq: int = 0,
    budget_base: int = 4000,
    as_of: datetime = DEFAULT_AS_OF,
    signer: Signer | None = None,
) -> CompileResult:
    """Compile one node's packet from its state, on the sidecar's compile path.

    Reads the objective and fabric blocks from ``state["objective"]`` and
    ``state["blocks"]``, then forwards every remaining argument 1:1 to
    ``compile_packet`` (stage comes from ``recipient.stage``). No network, no clock:
    identical inputs yield the identical packet hash and policy trace as calling
    ``compile_packet`` directly.
    """
    objective = state["objective"]
    if not isinstance(objective, str):
        raise TypeError(f"state['objective'] must be a str, got {type(objective).__name__}")
    blocks = tuple(state["blocks"])
    return compile_packet(
        scope=scope,
        recipient=recipient,
        objective=objective,
        stage=recipient.stage,
        blocks=blocks,
        watermark_event_seq=watermark_event_seq,
        budget_base=budget_base,
        as_of=as_of,
        signer=signer,
    )


def node_model_input(result: CompileResult) -> dict[str, str]:
    """The node's model input: the rendered packet as an OpenAI-shaped system message."""
    return {"role": "system", "content": render_packet(result.packet)}
