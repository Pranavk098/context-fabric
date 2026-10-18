"""LangGraph adapter — compile a node's ContextPacket via the sidecar's path, off a
LangGraph state, and hand the rendered packet to the node's model.

LangGraph is an *optional* extra (``langgraph`` in ``[project.optional-dependencies]``);
this package imports it lazily, and ``adapters.langgraph.intercept`` — the reusable core
— does not import LangGraph at all, so the rest of Context Fabric stays importable
without it. See ``docs/engineering/ADAPTERS.md``.
"""

from adapters.langgraph.intercept import compile_node_packet, node_model_input

__all__ = ["compile_node_packet", "node_model_input"]
