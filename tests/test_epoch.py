"""Sealed epoch chain tests — create/verify, tamper, replay, determinism."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from kernel.router.compiler import compile_packet
from protocol.canonical import canonicalize, packet_content_hash
from protocol.epoch import (
    Epoch,
    ReceiptMeta,
    create_epoch,
    epoch_content_hash,
    merkle_root,
    verify_chain,
    verify_epoch,
    verify_packet_in_epoch,
)
from protocol.models import ContextPacket, Recipient, Scope, Tier0Cartridge
from protocol.signing import Signer

SCOPE = Scope(tenant_id="t1", project_id="p1", workflow_id="w1", task_id="task1")
AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
SIG = Signer.from_seed_hex("00" * 32)
PUB = bytes.fromhex(SIG.public_hex)


def packet(content: str) -> ContextPacket:
    return compile_packet(
        scope=SCOPE,
        recipient=Recipient(agent_id="a1", role="implementer", stage="implement"),
        objective=content,
        stage="implement",
        blocks=[],
        as_of=AS_OF,
        signer=SIG,
    ).packet


def epoch(
    hashes: list[str], *, seq: int = 1, at: datetime = AS_OF, prev: str | None = None
) -> Epoch:
    return create_epoch(
        tenant_id="t1",
        watermark_event_seq=seq,
        packet_hashes=tuple(sorted(hashes)),
        created_at=at,
        signer=SIG,
        prev_epoch_hash=prev,
        meta=ReceiptMeta(model_id="test-model", tokenizer_id="o200k_base", engine="none"),
    )


def content_payload(ep: Epoch) -> bytes:
    data = ep.model_dump(mode="json")
    return canonicalize({k: v for k, v in data.items() if k not in ("signature", "key_id")})


def test_create_and_verify_happy_path() -> None:
    p1, p2 = packet("alpha"), packet("beta")
    ep = epoch([p1.hashes.content_hash, p2.hashes.content_hash])
    assert ep.epoch_id.startswith("epc_")
    assert ep.packet_count == 2
    assert ep.signature is not None
    assert verify_epoch(ep, PUB)
    assert verify_packet_in_epoch(ep, p1)
    assert verify_packet_in_epoch(ep, p2)
    meta = ep.meta
    assert meta is not None and meta.note == "output_reproduction_out_of_scope"


def test_flipped_character_in_packet_fails_membership() -> None:
    p1 = packet("alpha")
    ep = epoch([p1.hashes.content_hash])
    tampered = p1.model_copy(
        update={
            "tiers": p1.tiers.model_copy(
                update={
                    "tier0": Tier0Cartridge(
                        cartridge_id="cfx_none",
                        kv_prefix_hash="sha256:none",
                        content_markdown="x",
                    )
                }
            )
        }
    )
    assert packet_content_hash(tampered) != p1.hashes.content_hash
    assert verify_epoch(ep, PUB)
    assert not verify_packet_in_epoch(ep, tampered)


def test_epoch_field_tamper_fails_verification() -> None:
    ep = epoch([packet("alpha").hashes.content_hash])
    assert not verify_epoch(ep.model_copy(update={"tenant_id": "t2"}), PUB)
    assert not verify_epoch(ep.model_copy(update={"watermark_event_seq": 9}), PUB)
    assert not verify_epoch(ep.model_copy(update={"created_at": AS_OF + timedelta(days=1)}), PUB)
    assert not verify_epoch(ep.model_copy(update={"prev_epoch_hash": "sha256:deadbeef"}), PUB)
    assert not verify_epoch(ep.model_copy(update={"signature": None}), PUB)
    assert not verify_epoch(ep.model_copy(update={"key_id": "deadbeefdeadbeef"}), PUB)
    other = Signer.from_seed_hex("01" * 32)
    assert not verify_epoch(ep, bytes.fromhex(other.public_hex))


def test_valid_chain_verifies_but_reorder_and_prev_swap_fail() -> None:
    h1, h2, h3 = (packet(c).hashes.content_hash for c in ("alpha", "beta", "gamma"))
    e1 = epoch([h1], seq=1, at=AS_OF)
    e2 = epoch([h2], seq=2, at=AS_OF + timedelta(minutes=1), prev=epoch_content_hash(e1))
    e3 = epoch([h3], seq=3, at=AS_OF + timedelta(minutes=2), prev=epoch_content_hash(e2))
    assert verify_chain([e1, e2, e3], PUB)
    assert verify_chain([e1], PUB)
    assert not verify_chain([e1, e3, e2], PUB)
    assert not verify_chain([e2, e1, e3], PUB)
    assert not verify_chain([e3, e2, e1], PUB)
    genesis_swap = e2.model_copy(update={"prev_epoch_hash": None})
    assert not verify_chain([e1, genesis_swap, e3], PUB)
    tail_swap = e2.model_copy(update={"prev_epoch_hash": epoch_content_hash(e3)})
    assert not verify_chain([tail_swap, e3], PUB)
    cross_tenant = [
        e1,
        e2.model_copy(update={"tenant_id": "t2"}),
        e3.model_copy(update={"tenant_id": "t2"}),
    ]
    assert not verify_chain(cross_tenant, PUB)


def test_replay_duplicate_packet_hashes_rejected() -> None:
    h1, h2 = packet("alpha").hashes.content_hash, packet("beta").hashes.content_hash
    try:
        create_epoch(
            tenant_id="t1",
            watermark_event_seq=1,
            packet_hashes=(h1, h1, h2),
            created_at=AS_OF,
            signer=SIG,
        )
    except ValueError as exc:
        assert "sorted and duplicate-free" in str(exc)
    else:
        raise AssertionError("create_epoch must reject duplicate packet hashes")


def test_determinism_identical_inputs_identical_bytes() -> None:
    h = packet("alpha").hashes.content_hash
    a, b = epoch([h]), epoch([h])
    assert content_payload(a) == content_payload(b)
    assert a.epoch_id == b.epoch_id
    assert epoch_content_hash(a) == epoch_content_hash(b)


def test_merkle_root_rules() -> None:
    def leaf(x: str) -> bytes:
        return hashlib.sha256(x.encode("utf-8")).digest()

    def parent(*parts: bytes) -> str:
        return "sha256:" + hashlib.sha256(b"".join(parts)).hexdigest()

    a, b, c = "sha256:aa", "sha256:bb", "sha256:cc"
    assert merkle_root((a,)) == "sha256:" + leaf(a).hex()
    assert merkle_root((a, b)) == parent(leaf(a), leaf(b))
    assert merkle_root((b, a)) == merkle_root((a, b))
    assert merkle_root((a, b, c)) == parent(
        hashlib.sha256(leaf(a) + leaf(b)).digest(),
        hashlib.sha256(leaf(c) + leaf(c)).digest(),
    )
    assert merkle_root((a, b, c)) == merkle_root((c, a, b))
