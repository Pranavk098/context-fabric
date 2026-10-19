"""Map ``kernel.router.hints`` output to vLLM's merged ``KvHintsEnvelope`` (PR #53423).

vLLM PR #53423 (merged 2026-09-21, merge commit ``986e217``) added a first-class request
field ``kv_hints`` whose schema lives in ``vllm/v1/kv_hints/protocol.py``::

    KvHintsEnvelope(protocol_version: str, message_id: str, actions: list[KvHintAction])
    KvHintAction(action_id: str, action_type: str, action_version: str, payload: dict)

The envelope is transport plumbing only: vLLM defines no ``action_type`` semantics and its
built-in scheduler does not act on the hints (a downstream consumer such as KVCR #53624
would). We therefore emit exactly one action whose ``payload`` carries our flat hint
fields verbatim, so the mapping is lossless and round-trippable, and the ``action_type``
is namespaced to us so a future vLLM-defined action set cannot collide with it.
"""

from __future__ import annotations

from collections.abc import Mapping

from protocol.canonical import canonicalize, sha256_hex

# ``protocol_version``/``action_version`` mirror the values vLLM's own PR #53423 test
# fixtures use ("0.1" / "1.0"); the envelope schema leaves both as opaque strings.
PROTOCOL_VERSION = "0.1"
ACTION_TYPE = "context_fabric.cache_hint"
ACTION_VERSION = "1.0"
ACTION_ID = "cfx_cache_hint"
HTTP_FIELD = "kv_hints"  # request field name (proto ``GenerateRequest.kv_hints`` = 17)

_PRIORITIES = frozenset({"low", "normal", "high"})
_HINT_KEYS = frozenset(
    {"session_id", "prefix_hash", "shared_prefix_hash", "ttl_seconds", "evict_priority"}
)


def _validate_hints(hints: Mapping[str, object]) -> dict[str, str | int]:
    """Return the 5 mapped hint fields, raising ``ValueError`` on any invalid value."""
    missing = sorted(_HINT_KEYS - hints.keys())
    if missing:
        raise ValueError(f"kv hints missing field(s): {missing}")

    session_id = hints["session_id"]
    prefix_hash = hints["prefix_hash"]
    shared_prefix_hash = hints["shared_prefix_hash"]
    ttl_seconds = hints["ttl_seconds"]
    evict_priority = hints["evict_priority"]

    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")
    if not isinstance(prefix_hash, str) or not prefix_hash:
        raise ValueError("prefix_hash must be a non-empty string")
    if not isinstance(shared_prefix_hash, str):
        raise ValueError("shared_prefix_hash must be a string (may be empty)")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds < 0:
        raise ValueError("ttl_seconds must be a non-negative int")
    if not isinstance(evict_priority, str) or evict_priority not in _PRIORITIES:
        raise ValueError(f"evict_priority must be one of {sorted(_PRIORITIES)}")

    return {
        "session_id": session_id,
        "prefix_hash": prefix_hash,
        "shared_prefix_hash": shared_prefix_hash,
        "ttl_seconds": ttl_seconds,
        "evict_priority": evict_priority,
    }


def validate_kv_hints_envelope(envelope: object) -> None:
    """Raise ``ValueError`` unless ``envelope`` matches vLLM's ``KvHintsEnvelope`` shape."""
    if not isinstance(envelope, dict):
        raise ValueError("kv hints envelope must be an object")
    protocol_version = envelope.get("protocol_version")
    message_id = envelope.get("message_id")
    actions = envelope.get("actions")
    if not isinstance(protocol_version, str) or not protocol_version:
        raise ValueError("protocol_version must be a non-empty string")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("message_id must be a non-empty string")
    if not isinstance(actions, list):
        raise ValueError("actions must be a list")
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("each action must be an object")
        if not isinstance(action.get("action_id"), str) or not action["action_id"]:
            raise ValueError("action_id must be a non-empty string")
        if not isinstance(action.get("action_type"), str) or not action["action_type"]:
            raise ValueError("action_type must be a non-empty string")
        if not isinstance(action.get("action_version"), str) or not action["action_version"]:
            raise ValueError("action_version must be a non-empty string")
        if not isinstance(action.get("payload"), dict):
            raise ValueError("payload must be an object")


def build_kv_hints_envelope(hints: Mapping[str, object]) -> dict[str, object]:
    """Map a flat hint dict to a JSON-ready vLLM ``KvHintsEnvelope``.

    Deterministic: identical hints produce an identical envelope, including the
    content-derived ``message_id`` (keeps the compile path's ADR-0003 determinism). A live
    orchestrator needing per-request uniqueness would assign ``message_id`` itself.
    """
    payload: dict[str, str | int] = _validate_hints(hints)
    actions: list[dict[str, object]] = [
        {
            "action_id": ACTION_ID,
            "action_type": ACTION_TYPE,
            "action_version": ACTION_VERSION,
            "payload": payload,
        }
    ]
    envelope: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "message_id": "sha256:" + sha256_hex(canonicalize(actions)),
        "actions": actions,
    }
    validate_kv_hints_envelope(envelope)
    return envelope


def parse_kv_hints_envelope(envelope: object) -> dict[str, str | int]:
    """Round-trip a single-action envelope back to the flat hint dict (lossless)."""
    validate_kv_hints_envelope(envelope)
    assert isinstance(envelope, dict)  # validate_kv_hints_envelope guarantees this
    actions = envelope["actions"]
    assert isinstance(actions, list)
    if len(actions) != 1:
        raise ValueError("context_fabric envelopes carry exactly one action")
    action = actions[0]
    assert isinstance(action, dict)
    payload = action["payload"]
    assert isinstance(payload, dict)
    return _validate_hints(payload)
