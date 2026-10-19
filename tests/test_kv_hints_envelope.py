"""vLLM KvHintsEnvelope adapter — mapping + schema validation + round-trip."""

from __future__ import annotations

import pytest

from kernel.cartridge.serializer import Cartridge, count_tokens
from kernel.router import compile_packet
from kernel.router.hints import packet_cache_hints
from kernel.router.kv_hints_envelope import (
    ACTION_TYPE,
    ACTION_VERSION,
    HTTP_FIELD,
    PROTOCOL_VERSION,
    build_kv_hints_envelope,
    parse_kv_hints_envelope,
    validate_kv_hints_envelope,
)
from protocol.models import Recipient, Scope

_HINTS = {
    "session_id": "w1:task1",
    "prefix_hash": "sha256:aaaa",
    "shared_prefix_hash": "sha256:bbbb",
    "ttl_seconds": 3600,
    "evict_priority": "normal",
    "hint_version": "0.1",  # extra field must be dropped, not forwarded
}


def _compiled() -> dict[str, str | int]:
    cartridge = Cartridge(
        cartridge_id="cfx_p1_deadbeef",
        tenant_id="t1",
        project_id="p1",
        commit_hash="deadbeef00",
        hologram_markdown="# Hologram\n- core.py: shared()",
        token_count=count_tokens("# Hologram\n- core.py: shared()"),
        kv_prefix_hash="sha256:x",
    )
    result = compile_packet(
        scope=Scope(tenant_id="t1", project_id="p1", workflow_id="w1", task_id="task1"),
        recipient=Recipient(agent_id="a1", role="implementer", stage="implement"),
        objective="OBJECTIVE: test",
        stage="implement",
        blocks=[],
        cartridge=cartridge,
    )
    return packet_cache_hints(result.packet)


def test_envelope_matches_vllm_schema() -> None:
    envelope = build_kv_hints_envelope(dict(_HINTS))
    assert set(envelope) == {"protocol_version", "message_id", "actions"}
    assert envelope["protocol_version"] == PROTOCOL_VERSION == "0.1"
    assert isinstance(envelope["message_id"], str) and envelope["message_id"].startswith("sha256:")
    (action,) = envelope["actions"]  # type: ignore[attr-defined]
    assert set(action) == {"action_id", "action_type", "action_version", "payload"}
    assert action["action_type"] == ACTION_TYPE == "context_fabric.cache_hint"
    assert action["action_version"] == ACTION_VERSION == "1.0"
    assert action["payload"] == {
        "session_id": "w1:task1",
        "prefix_hash": "sha256:aaaa",
        "shared_prefix_hash": "sha256:bbbb",
        "ttl_seconds": 3600,
        "evict_priority": "normal",
    }
    assert "hint_version" not in action["payload"]


def test_envelope_round_trips_losslessly() -> None:
    assert parse_kv_hints_envelope(build_kv_hints_envelope(dict(_HINTS))) == {
        "session_id": "w1:task1",
        "prefix_hash": "sha256:aaaa",
        "shared_prefix_hash": "sha256:bbbb",
        "ttl_seconds": 3600,
        "evict_priority": "normal",
    }


def test_envelope_is_deterministic() -> None:
    assert build_kv_hints_envelope(dict(_HINTS)) == build_kv_hints_envelope(dict(_HINTS))


def test_message_id_changes_with_content() -> None:
    base = build_kv_hints_envelope(dict(_HINTS))
    other = build_kv_hints_envelope({**_HINTS, "ttl_seconds": 60})
    assert base["message_id"] != other["message_id"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda h: h.pop("session_id"),
        lambda h: h.update(session_id=""),
        lambda h: h.update(prefix_hash=""),
        lambda h: h.update(ttl_seconds=True),
        lambda h: h.update(ttl_seconds=-1),
        lambda h: h.update(evict_priority="urgent"),
        lambda h: h.update(shared_prefix_hash=None),
    ],
)
def test_invalid_hints_are_rejected(mutate) -> None:
    hints = dict(_HINTS)
    mutate(hints)
    with pytest.raises(ValueError):
        build_kv_hints_envelope(hints)


def test_empty_shared_prefix_hash_is_valid() -> None:
    envelope = build_kv_hints_envelope({**_HINTS, "shared_prefix_hash": ""})
    assert envelope["actions"][0]["payload"]["shared_prefix_hash"] == ""  # type: ignore[index]


def test_schema_validator_rejects_bad_envelopes() -> None:
    good = build_kv_hints_envelope(dict(_HINTS))
    validate_kv_hints_envelope(good)  # must not raise
    for bad in [
        None,
        [],
        {"protocol_version": "", "message_id": "m", "actions": []},
        {"protocol_version": "0.1", "message_id": "m", "actions": "nope"},
        {
            "protocol_version": "0.1",
            "message_id": "m",
            "actions": [{"action_id": "a", "action_type": "t", "action_version": "1.0"}],
        },  # missing payload
    ]:
        with pytest.raises(ValueError):
            validate_kv_hints_envelope(bad)


def test_integration_with_packet_cache_hints() -> None:
    hints = _compiled()
    envelope = build_kv_hints_envelope(hints)
    assert parse_kv_hints_envelope(envelope) == {
        "session_id": hints["session_id"],
        "prefix_hash": hints["prefix_hash"],
        "shared_prefix_hash": hints["shared_prefix_hash"],
        "ttl_seconds": hints["ttl_seconds"],
        "evict_priority": hints["evict_priority"],
    }


def test_http_field_name_is_kv_hints() -> None:
    assert HTTP_FIELD == "kv_hints"
