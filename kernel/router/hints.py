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

    Tier 0 (the cartridge hologram) renders first and the instruction block immediately
    after, so together they are the leading bytes of the rendered prompt. Everything after
    them differs per agent, per step, and per observation set, so including it would make
    the hash change on every turn and destroy prefix identity. Both strings are hashed as a
    canonicalized pair, so ``prefix_hash`` is order-insensitive and per-agent (the
    instruction differs across agents); ``shared_prefix_hash`` below isolates the Tier 0
    part, which is what agents on the same store/cartridge actually share.
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
    rendered prompt head (sha256 over the RFC 8785 canonical form of
    ``{"tier0": ..., "instruction": ...}`` — per-agent), ``shared_prefix_hash`` is sha256
    over the Tier 0 cartridge markdown alone — the token-prefix identity two agents on the
    same store could actually share, and ``""`` when there is no Tier 0 (small projects).
    ``ttl_seconds`` is the requested retention, and ``evict_priority`` is derived from how
    much of the packet budget the packet used: ``high`` at >= 75%, ``normal`` at >= 25%,
    else ``low`` (large cached contexts are the most expensive to rebuild).
    """
    usage = packet.total_tokens / max(packet.budget.hard_total_tokens, 1)
    if usage >= 0.75:
        priority = "high"
    elif usage >= 0.25:
        priority = "normal"
    else:
        priority = "low"

    tier0_markdown = packet.tiers.tier0.content_markdown
    # Same bytes/encoding the cartridge serializer hashes (kernel/cartridge/serializer.py),
    # so for a compiled cartridge this equals Tier0Cartridge.kv_prefix_hash.
    shared_prefix_hash = (
        "sha256:" + sha256_hex(tier0_markdown.encode("utf-8")) if tier0_markdown else ""
    )

    return {
        "session_id": f"{packet.scope.workflow_id}:{packet.scope.task_id}",
        "prefix_hash": "sha256:" + sha256_hex(canonicalize(_prefix_material(packet))),
        "shared_prefix_hash": shared_prefix_hash,
        "ttl_seconds": ttl_seconds,
        "evict_priority": priority,
        "hint_version": HINT_VERSION,
    }
