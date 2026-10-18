"""Build ContextBlocks from JSON payloads — writable memory for the sidecar.

Quarantine by default: a block written without ``trusted=true`` is stored as
``draft``, which the kernel policy gate denies (reason ``lifecycle:draft``).
Promotion happens at write time only (``cfx add --trusted`` / ``"trusted": true``);
in-place promotion is future work.
"""

from __future__ import annotations

import math
from typing import Any

from kernel.cartridge.serializer import count_tokens
from kernel.store import SqliteStore
from protocol.canonical import sha256_hex
from protocol.models import (
    BlockKind,
    ContextBlock,
    Creator,
    CreatorType,
    Fidelity,
    LifecycleStatus,
    MemoryTier,
)

_MAX_BLOCK_ID = 128


def _valid_block_id(block_id: str) -> bool:
    return 0 < len(block_id) <= _MAX_BLOCK_ID and not any(
        ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in block_id
    )


def stored_block(store: SqliteStore, block_id: str) -> ContextBlock | None:
    """Return the stored block with ``block_id`` (for truthful dedup responses)."""
    return next((b for b in store.load_blocks() if b.block_id == block_id), None)


def _symbols(payload: dict[str, Any], field: str) -> tuple[str, ...]:
    value = payload.get(field)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return tuple(value)


def build_block(
    payload: dict[str, Any],
    *,
    trusted: bool,
    event_seq: int,
    default_trust_domain: str = "default",
    default_agent: str = "default-agent",
) -> ContextBlock:
    """Build a ContextBlock from a POST /fabric/blocks payload.

    Requires ``block_kind`` and exactly one of ``content``/``ref_uri``. Raises
    ``ValueError`` on any invalid payload.
    """
    kind_raw = payload.get("block_kind")
    if not isinstance(kind_raw, str):
        raise ValueError("block_kind is required and must be a string")
    try:
        kind = BlockKind(kind_raw)
    except ValueError as exc:
        raise ValueError(f"unknown block_kind {kind_raw!r}") from exc

    content = payload.get("content")
    ref_uri = payload.get("ref_uri")
    if (content is None) == (ref_uri is None):
        raise ValueError("exactly one of content or ref_uri is required")
    if content is not None and not isinstance(content, str):
        raise ValueError("content must be a string")
    if ref_uri is not None and not isinstance(ref_uri, str):
        raise ValueError("ref_uri must be a string")
    text = content if content is not None else ref_uri
    assert isinstance(text, str)
    if not text.strip():
        raise ValueError("content or ref_uri must be non-empty")

    block_id = payload.get("block_id")
    if block_id is not None and not isinstance(block_id, str):
        raise ValueError("block_id must be a string")
    if block_id is None:
        block_id = "blk_" + sha256_hex((kind_raw + "\x00" + text).encode("utf-8"))[:16]
    elif not _valid_block_id(block_id):
        raise ValueError(
            f"block_id must be 1-{_MAX_BLOCK_ID} characters with no whitespace or control "
            "characters"
        )

    trust_domain = payload.get("trust_domain") or default_trust_domain
    if not isinstance(trust_domain, str):
        raise ValueError("trust_domain must be a string")
    creator_id = payload.get("creator_id") or default_agent
    if not isinstance(creator_id, str):
        raise ValueError("creator_id must be a string")
    confidence = payload.get("confidence", 1.0)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("confidence must be a number")
    if not math.isfinite(confidence):
        raise ValueError("confidence must be finite")

    return ContextBlock(
        block_id=block_id,
        block_kind=kind,
        memory_tier=MemoryTier.ledger,
        content=content,
        ref_uri=ref_uri,
        content_hash="sha256:" + sha256_hex(text.encode("utf-8")),
        token_estimate=count_tokens(text),
        source_event_seq=event_seq,
        creator=Creator(type=CreatorType.agent, id=creator_id),
        confidence=float(confidence),
        fidelity=Fidelity.extract,
        lifecycle_status=LifecycleStatus.routable if trusted else LifecycleStatus.draft,
        trust_domain=trust_domain,
        symbols_exported=_symbols(payload, "symbols_exported"),
        symbols_imported=_symbols(payload, "symbols_imported"),
    )
