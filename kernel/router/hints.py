"""Computable slice of the vLLM ``agent_hint`` channel (RFC #52113, still open).

The RFC asks applications to tell the engine what the engine cannot infer: which future
requests will reuse a prefix, how long it should live, and how eagerly it may be evicted.
The engine only sees tokens; the app knows the workflow.

Status: the RFC is OPEN and not shipped, so nothing consumes these hints yet. We produce
them deterministically from a compiled packet and record them in the compile trace
(``trace["cache_hints"]``). See ``docs/engineering/AGENT-HINTS.md`` for the field-to-RFC
mapping and the trigger for wiring them into a real engine.
"""

from __future__ import annotations

from protocol.canonical import canonicalize, sha256_hex
from protocol.models import ContextPacket

HINT_VERSION = "0.1"
DEFAULT_TTL_SECONDS = 3600


def _prefix_material(packet: ContextPacket) -> dict[str, str]:
    """Exactly the bytes whose identity a KV/prefix cache can reuse across packets.

    Only Tier 0 (the cartridge hologram) and the instruction block are included: they
    form the stable head of the rendered prompt. Everything after them differs per agent,
    per step, and per observation set, so including it would make the hash change on
    every turn and destroy prefix identity. Tier 1 decisions could be appended in a
    future format only if the renderer guaranteed them ahead of the instruction; it does
    not (instruction renders first).
    """
    instruction = packet.tiers.tier3.instruction
    instruction_content = ""
    if instruction is not None and instruction.content is not None:
        instruction_content = instruction.content
    return {
        "tier0": packet.tiers.tier0.content_markdown,
        "instruction": instruction_content,
    }


def packet_cache_hints(
    packet: ContextPacket, *, ttl_seconds: int = DEFAULT_TTL_SECONDS
) -> dict[str, str | int]:
    """Deterministic agent hints for one compiled packet (RFC #52113 shaped).

    ``session_id`` scopes reuse to the workflow/task pair, ``prefix_hash`` identifies the
    reusable prefix (sha256 over the RFC 8785 canonical form of
    ``{"tier0": ..., "instruction": ...}``), ``ttl_seconds`` is the requested retention,
    and ``evict_priority`` is derived from how much of the packet budget the packet used:
    ``high`` at >= 75%, ``normal`` at >= 25%, else ``low`` (large cached contexts are the
    most expensive to rebuild).
    """
    usage = packet.total_tokens / max(packet.budget.hard_total_tokens, 1)
    if usage >= 0.75:
        priority = "high"
    elif usage >= 0.25:
        priority = "normal"
    else:
        priority = "low"

    return {
        "session_id": f"{packet.scope.workflow_id}:{packet.scope.task_id}",
        "prefix_hash": "sha256:" + sha256_hex(canonicalize(_prefix_material(packet))),
        "ttl_seconds": ttl_seconds,
        "evict_priority": priority,
        "hint_version": HINT_VERSION,
    }
