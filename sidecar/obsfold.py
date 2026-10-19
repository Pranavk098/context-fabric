"""Observation folding for sub-agent windows — reversible ``ctx://obs/`` masking.

A spawned sub-agent on a small context window overflows because tool outputs pile up:
environment observations are 67-84% of multi-turn tokens (see
``docs/research/RESEARCH-ROUND-C-EXTERNAL.md`` §Q1/Q4). This module folds older tool
messages into a short, *reversible* stub and leaves the most recent ones verbatim, so the
window stops growing and nothing is lost.

Policy (``mask``):

* keep the last ``n`` observation (``role == "tool"``) messages verbatim (default 3);
* replace older ones with ``[ctx://obs/<sha256-12> folded: <tool>, <bytes> bytes,
  <first line>]``, storing the full content in a SQLite store keyed by the hash so it can
  be expanded later (``expand``).

Cache alignment (ReFold-style): the fold boundary only advances every ``k`` new
observations (default 3), so between fold events the rendered prompt is strictly
append-only — the leading bytes of consecutive requests stay identical and the prefix
cache survives. The window guard, given ``max_context_tokens``, folds more observations
(oldest first) until the request fits; it never touches system messages or the latest
user message (in fact it only ever touches ``tool`` messages, by construction).

Every fold event is appended to the store's append-only ``fold_log`` receipt (see
``kernel/store``), together with ``seen_hash`` — the sha256 of the exact folded message
list the model saw — so replay can reconstruct and verify it.

Deterministic: no clock (``created_at`` is a parameter), no RNG, input order is
authoritative, and stub ids are content-addressed. The ``first line`` in a stub is a
display hint truncated to ``_FIRST_LINE_MAX`` chars; reversibility depends only on the
sha256 id, not the hint.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kernel.cartridge.serializer import count_tokens
from kernel.store import SqliteStore
from protocol.canonical import canonicalize, sha256_hex
from protocol.models import BlockKind, ContextBlock, Creator, CreatorType, Fidelity, MemoryTier
from sidecar.blocks import stored_block

OBS_PREFIX = "ctx://obs/"
DEFAULT_N = 3
DEFAULT_K = 3
_FIRST_LINE_MAX = 80
_FRAME_TOKENS = 4  # per-message role/name/tool_call framing overhead (estimate)

# 48-bit id prefix from the content sha256; a collision would surface as a StoreConflict.
_SHORT_ID_HEX = 12


@dataclass(frozen=True)
class FoldEvent:
    """One folded observation, as recorded in the store's ``fold_log`` receipt."""

    message_index: int
    stub_id: str
    content_hash: str
    tool_name: str
    content_bytes: int


@dataclass(frozen=True)
class FoldResult:
    """Outcome of one fold pass over an OpenAI-style message list."""

    messages: list[dict[str, Any]]
    folded: int
    events: tuple[FoldEvent, ...]
    seen_hash: str


def _content_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _tool_name(messages: Sequence[dict[str, Any]], index: int) -> str:
    """Best-effort tool name: explicit ``name``, else the matching assistant ``tool_calls``."""
    message = messages[index]
    name = message.get("name")
    if isinstance(name, str) and name:
        return name
    call_id = message.get("tool_call_id")
    if isinstance(call_id, str) and call_id:
        for previous in reversed(messages[:index]):
            if previous.get("role") != "assistant":
                continue
            calls = previous.get("tool_calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict) or call.get("id") != call_id:
                    continue
                function = call.get("function")
                if not isinstance(function, dict):
                    continue
                function_name = function.get("name")
                if isinstance(function_name, str):
                    return function_name
    return "tool"


def make_stub(content: str, tool_name: str) -> tuple[str, str]:
    """Return ``(stub, stub_id)`` for one observation's content."""
    content_bytes = content.encode("utf-8")
    stub_id = sha256_hex(content_bytes)[:_SHORT_ID_HEX]
    first_line = content.split("\n", 1)[0]
    if len(first_line) > _FIRST_LINE_MAX:
        first_line = first_line[:_FIRST_LINE_MAX] + "\u2026"
    stub = f"[ctx://obs/{stub_id} folded: {tool_name}, {len(content_bytes)} bytes, {first_line}]"
    return stub, stub_id


def _foldable_indices(messages: Sequence[dict[str, Any]]) -> list[int]:
    """Indices of tool messages with string content — the only messages folding touches."""
    return [
        index
        for index, message in enumerate(messages)
        if isinstance(message, dict)
        and message.get("role") == "tool"
        and isinstance(message.get("content"), str)
    ]


def _message_tokens(message: dict[str, Any]) -> int:
    return count_tokens(_content_text(message)) + _FRAME_TOKENS


def estimate_tokens(messages: Sequence[dict[str, Any]]) -> int:
    """Token estimate for a message list (o200k_base counts plus a framing constant)."""
    return sum(_message_tokens(message) for message in messages)


def _folded_estimate(
    messages: Sequence[dict[str, Any]], fold_positions: set[int], stubs: dict[int, str]
) -> int:
    total = 0
    for index, message in enumerate(messages):
        if index in fold_positions:
            total += count_tokens(stubs[index]) + _FRAME_TOKENS
        else:
            total += _message_tokens(message)
    return total


def _block_id_from_uri(uri: str) -> str:
    if uri.startswith(OBS_PREFIX):
        return "obs_" + uri[len(OBS_PREFIX) :]
    if uri.startswith("obs_"):
        return uri
    return "obs_" + uri


def expand(store: SqliteStore, uri: str) -> str | None:
    """Expand a ``ctx://obs/<id>`` (or bare ``obs_<id>``) stub back to its stored content."""
    block = stored_block(store, _block_id_from_uri(uri))
    return block.content if block is not None else None


def _fold_event(message_index: int, stub_id: str, content: str, tool_name: str) -> FoldEvent:
    return FoldEvent(
        message_index=message_index,
        stub_id=stub_id,
        content_hash="sha256:" + sha256_hex(content.encode("utf-8")),
        tool_name=tool_name,
        content_bytes=len(content.encode("utf-8")),
    )


def fold_messages(
    messages: Sequence[dict[str, Any]],
    *,
    n: int = DEFAULT_N,
    k: int = DEFAULT_K,
    max_context_tokens: int | None = None,
    store: SqliteStore | None = None,
    session_id: str = "default",
    created_at: datetime | None = None,
) -> FoldResult:
    """Fold older tool observations into reversible stubs; return the folded list + receipt.

    ``n``/``k`` are the mask and chunk sizes; ``max_context_tokens`` enables the window
    guard (fold more, oldest first, until the estimate fits). When ``store`` is given the
    folded content is persisted (idempotently, keyed by content hash) and every fold event
    is appended to the store's ``fold_log`` receipt. Only ``tool`` messages are ever
    folded; system messages and the latest user message are never touched.
    """
    if n < 0:
        raise ValueError("n must be >= 0")
    if k < 1:
        raise ValueError("k must be >= 1")

    foldable = _foldable_indices(messages)
    total = len(foldable)

    folded_by_mask = 0
    if total > n:
        folded_by_mask = ((total - n) // k) * k
    fold_positions: set[int] = set(foldable[:folded_by_mask])

    # Precompute stubs and event facts for every foldable observation (content-addressed).
    stubs: dict[int, str] = {}
    facts: dict[int, tuple[str, str, str, int]] = {}  # idx -> (stub_id, tool_name, hash, bytes)
    for index in foldable:
        content = messages[index]["content"]
        assert isinstance(content, str)
        tool_name = _tool_name(messages, index)
        stub, stub_id = make_stub(content, tool_name)
        stubs[index] = stub
        facts[index] = (
            stub_id,
            tool_name,
            "sha256:" + sha256_hex(content.encode("utf-8")),
            len(content.encode("utf-8")),
        )

    # Window guard: fold more, oldest first, until the estimate fits (best effort).
    if max_context_tokens is not None:
        remaining = [index for index in foldable if index not in fold_positions]
        while remaining and _folded_estimate(messages, fold_positions, stubs) > max_context_tokens:
            fold_positions.add(remaining.pop(0))

    output = [dict(message) for message in messages]
    for index in sorted(fold_positions):
        output[index]["content"] = stubs[index]

    events = tuple(
        _fold_event(index, facts[index][0], messages[index]["content"], facts[index][1])
        for index in sorted(fold_positions)
    )
    seen_hash = "sha256:" + sha256_hex(canonicalize(output))

    if store is not None and events:
        base_seq = store.max_source_event_seq()
        blocks = [
            ContextBlock(
                block_id="obs_" + facts[index][0],
                block_kind=BlockKind.observation,
                memory_tier=MemoryTier.register,
                content=messages[index]["content"],
                content_hash=facts[index][2],
                token_estimate=count_tokens(messages[index]["content"]),
                source_event_seq=base_seq + offset,
                creator=Creator(type=CreatorType.tool, id=facts[index][1]),
                fidelity=Fidelity.raw,
                trust_domain="default",
            )
            for offset, index in enumerate(sorted(fold_positions), start=1)
        ]
        store.append_many(blocks)
        store.log_folds(
            session_id,
            [
                {
                    "message_index": event.message_index,
                    "stub_id": event.stub_id,
                    "content_hash": event.content_hash,
                    "tool_name": event.tool_name,
                    "content_bytes": event.content_bytes,
                }
                for event in events
            ],
            seen_hash,
            created_at or datetime.now(tz=UTC),
        )

    return FoldResult(messages=output, folded=len(events), events=events, seen_hash=seen_hash)


if __name__ == "__main__":
    # Self-check: mask, reversibility via an in-memory store, and prefix stability.
    def messages(count: int) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = [
            {"role": "system", "content": "you are a coding sub-agent"},
            {"role": "user", "content": "fix the bug"},
        ]
        for i in range(count):
            msgs.append({"role": "assistant", "content": f"calling tool {i}"})
            body = " ".join(f"body {i} {j}" for j in range(50))
            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": f"call_{i}",
                    "name": "read_file",
                    "content": f"line one of file {i}\n{body}",
                }
            )
        return msgs

    def render(msgs: Sequence[dict[str, Any]]) -> bytes:
        return b"\n".join(canonicalize(message) for message in msgs)

    store = SqliteStore(":memory:")
    result = fold_messages(messages(6), store=store, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    assert result.folded == 3, "6 obs with n=3,k=3 folds the 3 oldest"
    first_content = messages(6)[3]["content"]
    assert expand(store, "ctx://obs/" + result.events[0].stub_id) == first_content
    # last 3 observations verbatim (indices 13, 11, 9 are tools call_5..call_3)
    assert result.messages[-1]["content"] == messages(6)[-1]["content"]
    assert result.messages[-3]["content"] == messages(6)[-3]["content"]
    assert result.messages[-5]["content"] == messages(6)[-5]["content"]
    # prefix stability between folds: 6 -> 7 -> 8 append-only leading bytes
    r6 = render(fold_messages(messages(6)).messages)
    r7 = render(fold_messages(messages(7)).messages)
    r8 = render(fold_messages(messages(8)).messages)
    assert r7.startswith(r6) and r8.startswith(r7)
    # fold boundary at 9 doubles the folded count
    assert fold_messages(messages(9)).folded == 6
    # window guard folds until it fits, never touching system/latest user
    floor = estimate_tokens(messages(2))
    guarded = fold_messages(messages(6), n=0, max_context_tokens=floor)
    assert estimate_tokens(guarded.messages) <= floor
    assert guarded.messages[0]["content"] == messages(6)[0]["content"]
    assert guarded.messages[1]["content"] == messages(6)[1]["content"]
    print("obsfold.py self-check: OK (mask, expand, prefix stability, window guard)")
