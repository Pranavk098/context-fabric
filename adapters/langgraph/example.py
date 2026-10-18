"""A real 3-node LangGraph workflow (planner -> worker -> reviewer) over the adapter.

Each node compiles its own ContextPacket from the shared fabric via
``compile_node_packet`` (the sidecar's compile path) and hands the rendered packet to its
chat model as the system message. Runs fully offline with a deterministic stub model; set
``CF_UPSTREAM`` to a live OpenAI-compatible base URL to hit a real model instead.

Run: ``uv run python -m adapters.langgraph.example`` (needs the ``langgraph`` extra).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol, TypedDict

import httpx
from langgraph.graph import END, START, StateGraph

from adapters.langgraph import compile_node_packet, node_model_input
from kernel.cartridge.serializer import count_tokens
from kernel.router.compiler import DEFAULT_AS_OF, CompileResult
from protocol.canonical import sha256_hex
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

SCOPE = Scope(tenant_id="demo", project_id="demo", workflow_id="review", task_id="t1")
PLANNER = Recipient(agent_id="planner", role="planner", stage="plan")
WORKER = Recipient(agent_id="worker", role="implementer", stage="implement")
REVIEWER = Recipient(agent_id="reviewer", role="reviewer", stage="review")
OBJECTIVE = "plan and implement the storage migration"
BLOCK_AS_OF = datetime(2026, 1, 1, tzinfo=UTC)


class State(TypedDict):
    objective: str
    blocks: list[ContextBlock]
    plan: str
    work: str
    verdict: str


class ChatModel(Protocol):
    def invoke(self, messages: list[dict[str, str]]) -> str: ...


class FakeChatModel:
    """Deterministic offline stand-in: echoes how much context it was handed."""

    def invoke(self, messages: list[dict[str, str]]) -> str:
        system = messages[0]["content"]
        return f"stub reply to {len(messages)} message(s), {len(system)} system chars"


class UpstreamChatModel:
    """A live OpenAI-compatible upstream, reached over plain httpx (no SDK)."""

    def __init__(self, base_url: str, model: str = "gpt-4o-mini") -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model

    def invoke(self, messages: list[dict[str, str]]) -> str:
        resp = httpx.post(
            f"{self._base_url}/v1/chat/completions",
            json={"model": self._model, "messages": messages},
            timeout=60.0,
        )
        resp.raise_for_status()
        return str(resp.json()["choices"][0]["message"]["content"])


def _block(
    block_id: str,
    kind: BlockKind,
    content: str,
    *,
    seq: int,
    denied_roles: tuple[str, ...] = (),
    symbols: tuple[str, ...] = (),
) -> ContextBlock:
    return ContextBlock(
        block_id=block_id,
        block_kind=kind,
        memory_tier=MemoryTier.ledger,
        content=content,
        content_hash="sha256:" + sha256_hex(content.encode("utf-8")),
        token_estimate=count_tokens(content),
        source_event_seq=seq,
        creator=Creator(type=CreatorType.agent, id="demo"),
        fidelity=Fidelity.extract,
        trust_domain=SCOPE.tenant_id,
        observed_at=BLOCK_AS_OF,
        valid_from=BLOCK_AS_OF,
        denied_roles=denied_roles,
        symbols_exported=symbols,
    )


def demo_blocks() -> list[ContextBlock]:
    """The shared fabric memory the three agents compile from.

    ``obs1`` is denied to the reviewer (role blindness), so the reviewer's packet and
    policy trace diverge from planner/worker — the divergent-role shape B4 builds on.
    """
    return [
        _block("d1", BlockKind.decision, "Use Postgres for canonical storage", seq=1),
        _block("f1", BlockKind.fact, "The context compiler is deterministic", seq=2),
        _block(
            "obs1",
            BlockKind.observation,
            "implementer-only rationale for storage prompt sizing",
            seq=3,
            denied_roles=("reviewer",),
        ),
        _block(
            "nk1",
            BlockKind.negative_knowledge,
            "Do not retry naive masking",
            seq=4,
            symbols=("masking",),
        ),
    ]


def build_graph(
    *,
    scope: Scope = SCOPE,
    as_of: datetime = DEFAULT_AS_OF,
    model: ChatModel | None = None,
) -> tuple[Any, list[tuple[str, CompileResult]]]:
    """Build the planner -> worker -> reviewer graph.

    Returns the compiled graph plus a per-node ``(name, CompileResult)`` log filled during
    ``invoke`` (a closure, not state, so it survives LangGraph's state copying).
    """
    chat: ChatModel = model or _make_model()
    results: list[tuple[str, CompileResult]] = []

    def planner(state: Mapping[str, Any]) -> dict[str, str]:
        result = compile_node_packet(state, scope=scope, recipient=PLANNER, as_of=as_of)
        results.append(("planner", result))
        objective = str(state["objective"])
        plan = chat.invoke([node_model_input(result), {"role": "user", "content": objective}])
        return {"plan": plan}

    def worker(state: Mapping[str, Any]) -> dict[str, str]:
        result = compile_node_packet(state, scope=scope, recipient=WORKER, as_of=as_of)
        results.append(("worker", result))
        objective = str(state["objective"])
        work = chat.invoke([node_model_input(result), {"role": "user", "content": objective}])
        return {"work": work}

    def reviewer(state: Mapping[str, Any]) -> dict[str, str]:
        result = compile_node_packet(state, scope=scope, recipient=REVIEWER, as_of=as_of)
        results.append(("reviewer", result))
        objective = str(state["objective"])
        verdict = chat.invoke([node_model_input(result), {"role": "user", "content": objective}])
        return {"verdict": verdict}

    graph = StateGraph(State)
    graph.add_node("planner", planner)
    graph.add_node("worker", worker)
    graph.add_node("reviewer", reviewer)
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "worker")
    graph.add_edge("worker", "reviewer")
    graph.add_edge("reviewer", END)
    return graph.compile(), results


def _make_model() -> ChatModel:
    upstream = os.environ.get("CF_UPSTREAM")
    if upstream:
        return UpstreamChatModel(upstream)
    return FakeChatModel()


def main() -> None:
    graph, results = build_graph()
    final = graph.invoke({"objective": OBJECTIVE, "blocks": demo_blocks()})
    print(f"objective: {OBJECTIVE}")
    print(f"model: {type(_make_model()).__name__}")
    for name, result in results:
        packet = result.packet
        print(
            f"{name}: packet={packet.packet_id} hash={packet.hashes.content_hash} "
            f"total_tokens={packet.total_tokens} excluded={len(result.trace['excluded'])}"
        )
    print(f"verdict: {final.get('verdict')}")


if __name__ == "__main__":
    main()
