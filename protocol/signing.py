"""Ed25519 packet signing (CPP v1.0, audit-safe key identification).

Signing is off the critical path: the `hashes` block is excluded from `content_hash`
(protocol.canonical.packet_content_hash), so signatures never feed the hash.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from protocol.canonical import sha256_hex


class Signer:
    """Wraps an Ed25519 private key; exposes a stable ``key_id``."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._priv = private_key

    @classmethod
    def generate(cls) -> Signer:
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_seed_hex(cls, seed_hex: str) -> Signer:
        return cls(Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex)))

    @property
    def public_hex(self) -> str:
        raw = self._priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        return raw.hex()

    @property
    def key_id(self) -> str:
        return sha256_hex(bytes.fromhex(self.public_hex))[:16]

    def sign(self, data: bytes) -> str:
        return self._priv.sign(data).hex()

    def verify(self, data: bytes, signature_hex: str) -> bool:
        return verify_signature(self.public_hex, data, signature_hex)


def verify_signature(public_hex: str, data: bytes, signature_hex: str) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
        pub.verify(bytes.fromhex(signature_hex), data)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    s = Signer.from_seed_hex("00" * 32)
    sig = s.sign(b"context-fabric")
    assert s.verify(b"context-fabric", sig)
    assert not s.verify(b"tampered", sig)
    assert len(s.key_id) == 16
    print(f"signing.py self-check: OK (key_id={s.key_id})")
