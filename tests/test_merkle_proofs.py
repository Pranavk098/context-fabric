"""Compact Merkle inclusion proofs — encoding, verification, tamper, JSON round-trip."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from kernel.router.compiler import compile_packet
from protocol.epoch import (
    Epoch,
    create_epoch,
    merkle_proof,
    merkle_root,
    verify_merkle_proof,
    verify_packet_in_epoch,
)
from protocol.models import ContextPacket, Recipient, Scope
from protocol.signing import Signer

SCOPE = Scope(tenant_id="t1", project_id="p1", workflow_id="w1", task_id="task1")
AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
SIG = Signer.from_seed_hex("00" * 32)


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


def synthetic_hash(tag: str) -> str:
    return "sha256:" + hashlib.sha256(tag.encode("utf-8")).hexdigest()


def leaf_digest(packet_hash: str) -> str:
    return "sha256:" + hashlib.sha256(packet_hash.encode("utf-8")).hexdigest()


def epoch_for(hashes: list[str]) -> Epoch:
    return create_epoch(
        tenant_id="t1",
        watermark_event_seq=1,
        packet_hashes=tuple(sorted(hashes)),
        created_at=AS_OF,
        signer=SIG,
    )


def test_every_leaf_proof_verifies_sizes_1_to_12() -> None:
    for size in range(1, 13):
        hashes = sorted(synthetic_hash(f"p{i}") for i in range(size))
        ep = epoch_for(hashes)
        assert ep.merkle_root == merkle_root(tuple(hashes))
        for packet_hash in hashes:
            proof = merkle_proof(ep, packet_hash)
            assert proof is not None
            assert len(proof) <= (size - 1).bit_length() + 1
            assert len(proof) == (size - 1).bit_length()
            for element in proof:
                assert element[:2] in ("L:", "R:")
                assert element[2:].startswith("sha256:")
            assert verify_merkle_proof(packet_hash, proof, ep.merkle_root)


def test_hash_not_in_epoch_returns_none() -> None:
    ep = epoch_for([synthetic_hash("p0"), synthetic_hash("p1")])
    assert merkle_proof(ep, synthetic_hash("nope")) is None


def test_wrong_leaf_does_not_verify() -> None:
    for size in range(2, 13):
        hashes = sorted(synthetic_hash(f"p{i}") for i in range(size))
        ep = epoch_for(hashes)
        proof = merkle_proof(ep, hashes[0])
        assert proof is not None
        for other in hashes[1:]:
            assert not verify_merkle_proof(other, proof, ep.merkle_root)
        assert not verify_merkle_proof(synthetic_hash("outsider"), proof, ep.merkle_root)


def test_flipped_character_in_any_proof_element_fails() -> None:
    hashes = sorted(synthetic_hash(f"p{i}") for i in range(9))
    ep = epoch_for(hashes)
    for packet_hash in hashes:
        proof = merkle_proof(ep, packet_hash)
        assert proof is not None
        for i, element in enumerate(proof):
            digest_flip = element[:-1] + ("0" if element[-1] != "0" else "1")
            side_flip = ("R" if element[0] == "L" else "L") + element[1:]
            assert not verify_merkle_proof(
                packet_hash, proof[:i] + (digest_flip,) + proof[i + 1 :], ep.merkle_root
            )
            assert not verify_merkle_proof(
                packet_hash, proof[:i] + (side_flip,) + proof[i + 1 :], ep.merkle_root
            )


def test_empty_proof_valid_only_when_leaf_is_root() -> None:
    only = synthetic_hash("only")
    ep = epoch_for([only])
    assert merkle_proof(ep, only) == ()
    assert verify_merkle_proof(only, (), ep.merkle_root)
    assert not verify_merkle_proof(synthetic_hash("other"), (), ep.merkle_root)

    pair = epoch_for([synthetic_hash("a"), synthetic_hash("b")])
    for packet_hash in pair.packet_hashes:
        proof = merkle_proof(pair, packet_hash)
        assert proof
        assert not verify_merkle_proof(packet_hash, (), pair.merkle_root)


def test_odd_carry_leaf_pairs_with_itself() -> None:
    hashes = sorted(synthetic_hash(f"p{i}") for i in range(3))
    ep = epoch_for(hashes)
    proof = merkle_proof(ep, hashes[2])
    assert proof is not None
    assert proof[0] == "R:" + leaf_digest(hashes[2])
    l0, l1, l2 = (bytes.fromhex(leaf_digest(h)[len("sha256:") :]) for h in hashes)
    parent_01 = hashlib.sha256(l0 + l1).digest()
    parent_22 = hashlib.sha256(l2 + l2).digest()
    assert proof == ("R:" + leaf_digest(hashes[2]), "L:sha256:" + parent_01.hex())
    assert ep.merkle_root == "sha256:" + hashlib.sha256(parent_01 + parent_22).hexdigest()


def test_membership_check_unchanged_and_proof_interops() -> None:
    packets = [packet(content) for content in ("alpha", "beta", "gamma")]
    ep = epoch_for([p.hashes.content_hash for p in packets])
    for p in packets:
        assert verify_packet_in_epoch(ep, p)
        proof = merkle_proof(ep, p.hashes.content_hash)
        assert proof is not None
        assert verify_merkle_proof(p.hashes.content_hash, proof, ep.merkle_root)


def test_proofs_survive_json_round_trip() -> None:
    hashes = sorted(synthetic_hash(f"p{i}") for i in range(7))
    ep = epoch_for(hashes)
    restored = Epoch.model_validate(json.loads(json.dumps(ep.model_dump(mode="json"))))
    assert restored == ep
    for packet_hash in restored.packet_hashes:
        proof = merkle_proof(restored, packet_hash)
        assert proof is not None
        assert proof == merkle_proof(ep, packet_hash)
        assert verify_merkle_proof(packet_hash, proof, restored.merkle_root)


def test_malformed_proof_elements_fail_closed() -> None:
    hashes = sorted(synthetic_hash(f"p{i}") for i in range(3))
    ep = epoch_for(hashes)
    packet_hash = hashes[0]
    valid = merkle_proof(ep, packet_hash)
    assert valid is not None
    bad_element = "L:sha256:" + "0" * 62
    for bad in ("", "L", "L:", "X:sha256:" + "00" * 32, "L:sha256:", "L:sha256:zz", bad_element):
        assert not verify_merkle_proof(packet_hash, (bad,), ep.merkle_root)
    for bad_type in (123, None, b"L:sha256:" + b"00" * 32):
        assert not verify_merkle_proof(packet_hash, (bad_type,), ep.merkle_root)  # type: ignore[arg-type]
    assert not verify_merkle_proof(packet_hash, valid + ("R:sha256:" + "00" * 32,), ep.merkle_root)
