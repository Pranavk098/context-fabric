"""Ed25519 signing tests."""

from __future__ import annotations

from protocol.signing import Signer, verify_signature


def test_sign_and_verify_roundtrip() -> None:
    signer = Signer.from_seed_hex("00" * 32)
    sig = signer.sign(b"context-fabric")
    assert signer.verify(b"context-fabric", sig)
    assert not signer.verify(b"tampered", sig)
    assert verify_signature(signer.public_hex, b"context-fabric", sig)
    assert len(signer.key_id) == 16


def test_key_id_is_stable() -> None:
    a = Signer.from_seed_hex("01" * 32)
    b = Signer.from_seed_hex("01" * 32)
    assert a.key_id == b.key_id
    assert a.public_hex == b.public_hex
