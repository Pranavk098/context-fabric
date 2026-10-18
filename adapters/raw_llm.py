"""Provider-shaped request fragments for a compiled packet (v0: OpenAI + Anthropic).

Cache mechanics differ per provider: OpenAI caches identical leading bytes automatically
(nothing to set), while Anthropic caches only up to explicit ``cache_control``
breakpoints, so ``to_anthropic`` splits the system prompt into the shared Tier 0 block
(pinned) and the per-agent remainder. The renderer
(``sidecar.render.render_packet``) already emits Tier 0 first, so both adapters carry the
same rendered text and the shared prefix is a leading substring of both. See
``docs/engineering/ADAPTERS.md`` for the shapes, the layering note, and the limits.
"""

from __future__ import annotations

from typing import Literal

from protocol.models import ContextPacket
from sidecar.render import render_packet

__all__ = [
    "shared_prefix",
    "openai_system_message",
    "openai_request_extras",
    "to_anthropic",
]


def shared_prefix(packet: ContextPacket) -> str:
    """The exact Tier 0 bytes the Anthropic breakpoint pins and OpenAI's cache keys on.

    ``render_packet`` emits this string as the leading bytes of the render (asserted in
    ``tests/test_adapters.py``), and it is identical for every agent on the same
    store/cartridge — the same bytes ``shared_prefix_hash`` hashes
    (``kernel/router/hints.py``). Empty when the packet has no cartridge; nothing is
    then shared across agents to pin.
    """
    return packet.tiers.tier0.content_markdown


def openai_system_message(packet: ContextPacket) -> dict[str, str]:
    """The system message OpenAI's Chat Completions API takes, rendered for cache reuse."""
    return {"role": "system", "content": render_packet(packet)}


def openai_request_extras(packet: ContextPacket) -> dict[str, object]:
    """Always ``{}``: OpenAI prompt caching needs no request parameters from us.

    OpenAI caches the exact leading bytes of the request automatically (minimum ~1,024
    tokens), and ``render_packet`` already emits the shared Tier 0 cartridge markdown
    first, so prefix identity happens by construction. The Chat Completions API has no
    ``cache_control`` equivalent to set (checked 2026-10-18); ``prompt_cache_key``
    exists but is a routing/stability knob for cache placement, deliberately out of v0.
    The function exists so call sites keep a stable ``{**body, **extras}`` shape if a
    future provider revision adds a cache parameter.
    """
    return {}


def _cache_control(cache_ttl: Literal["5m", "1h"] | None) -> dict[str, str]:
    """Anthropic ``cache_control`` object; TTL field shape per the Messages API docs."""
    if cache_ttl is None:
        return {"type": "ephemeral"}  # documented default: 5-minute TTL
    if cache_ttl not in ("5m", "1h"):
        raise ValueError(f"cache_ttl must be '5m', '1h' or None; got {cache_ttl!r}")
    return {"type": "ephemeral", "ttl": cache_ttl}


def to_anthropic(
    packet: ContextPacket, cache_ttl: Literal["5m", "1h"] | None = None
) -> dict[str, list[dict[str, object]]]:
    """Request fragment for the Anthropic Messages API: ``{"system": [<blocks>]}``.

    With a cartridge, the system prompt is split so the cache breakpoint sits exactly at
    the end of the shared Tier 0 block: block 1 is ``shared_prefix(packet)`` with
    ``cache_control: {"type": "ephemeral"}`` (Anthropic caches only up to an explicit
    breakpoint), block 2 is the rest of the rendered text, unpinned. Joining the block
    texts with ``"\\n"`` reproduces ``render_packet(packet)`` byte for byte. Without a
    cartridge there is nothing shared to pin, so a single unpinned block is returned and
    ``cache_ttl`` is ignored. ``cache_ttl`` maps to the documented ``cache_control.ttl``
    field (``"5m"`` default, ``"1h"`` extended); ``None`` emits the plain default form.
    Deterministic: same packet, same env, same dict — no clock, no RNG.
    """
    rendered = render_packet(packet)
    prefix = shared_prefix(packet)
    blocks: list[dict[str, object]] = []
    if prefix:
        blocks.append({"type": "text", "text": prefix, "cache_control": _cache_control(cache_ttl)})
        rest = rendered[len(prefix) :].removeprefix("\n")
        if rest:  # Anthropic rejects empty text blocks (minLength 1)
            blocks.append({"type": "text", "text": rest})
    elif rendered:
        blocks.append({"type": "text", "text": rendered})
    return {"system": blocks}
