"""Sealed epochs: signed, chained receipts over batches of compiled context packets.

The seal minimum (docs/engineering/SEAL.md, docs/research/PROVENANCE-PRIOR-ART.md):
each packet is already RFC 8785-canonical-hashed + Ed25519-signed; an epoch batches
packet content hashes under a deterministic Merkle root, links to the previous epoch
(``prev_epoch_hash``), and is itself signed. This proves which context bytes were
handed to an agent and that they were never altered. It makes no claim about the
model's answer (see ``ReceiptMeta.note``).

Determinism (ADR-0003): ``created_at`` is an explicit parameter, never wall-clock;
``epoch_content_hash`` excludes ``signature``, ``key_id``, and ``epoch_id`` (the last is
derived from the hash itself), so the hash is signature-independent and repeatable.
Signing payload = the UTF-8 bytes of the content hash string (same convention as packet
signing in kernel/router/compiler.py).

Merkle rule: leaf digest = ``sha256(utf8(content-hash string))``; a parent is
``sha256(left_digest + right_digest)`` over raw 32-byte digests; at an odd level the last
node is duplicated (paired with itself) before hashing the next level; the root of a
single leaf is that leaf's digest. All digest bytes are concatenated raw (not
hex/lowercase strings).
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from protocol.canonical import canonicalize, packet_content_hash, sha256_hex
from protocol.models import ContextPacket
from protocol.signing import Signer, verify_signature

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_NOTE = "output_reproduction_out_of_scope"


class ReceiptMeta(BaseModel):
    """Inference facts recorded as metadata, not guarantees (prior-art rec. 3)."""

    model_config = _FROZEN

    model_id: str
    tokenizer_id: str
    engine: str
    note: str = _NOTE


class Epoch(BaseModel):
    model_config = _FROZEN

    epoch_id: str
    created_at: datetime
    tenant_id: str
    watermark_event_seq: int = Field(ge=0)
    prev_epoch_hash: str | None = None
    packet_hashes: tuple[str, ...]
    merkle_root: str
    packet_count: int = Field(ge=0)
    key_id: str | None = None
    signature: str | None = None
    meta: ReceiptMeta | None = None

    @model_validator(mode="after")
    def _counts_match(self) -> Epoch:
        if self.packet_count != len(self.packet_hashes):
            raise ValueError("packet_count must equal len(packet_hashes)")
        return self


def merkle_root(hashes: tuple[str, ...]) -> str:
    """Deterministic binary Merkle root (``sha256:``-prefixed) over leaf hashes (sorted here)."""
    if not hashes:
        raise ValueError("merkle_root requires at least one hash")
    level = [hashlib.sha256(leaf.encode("utf-8")).digest() for leaf in sorted(hashes)]
    while len(level) > 1:
        if len(level) % 2:
            level = level + [level[-1]]
        level = [hashlib.sha256(level[i] + level[i + 1]).digest() for i in range(0, len(level), 2)]
    return "sha256:" + level[0].hex()


def merkle_proof(epoch: Epoch, packet_hash: str) -> tuple[str, ...] | None:
    """Compact inclusion proof for ``packet_hash`` under ``epoch.merkle_root``.

    Returns the sibling path in leaf -> root order, or ``None`` when the hash is not in
    ``epoch.packet_hashes``. Each element is ``"L:sha256:<hex>"`` or ``"R:sha256:<hex>"``:
    the digest is the sibling node (same ``sha256:``-prefixed hex format as the root) and
    the side is that sibling's position, so the leaf index is implicit in the L/R sequence
    (``L``: parent = sha256(sibling + node), ``R``: parent = sha256(node + sibling)). A
    single leaf has the empty proof. Follows ``merkle_root``'s rules exactly: leaves sorted,
    raw 32-byte digests concatenated, an odd level duplicates its last node.
    """
    leaves = sorted(epoch.packet_hashes)
    if packet_hash not in leaves:
        return None
    index = leaves.index(packet_hash)
    level = [hashlib.sha256(leaf.encode("utf-8")).digest() for leaf in leaves]
    proof: list[str] = []
    while len(level) > 1:
        if index % 2:
            proof.append("L:sha256:" + level[index - 1].hex())
        else:
            sibling = level[index + 1] if index + 1 < len(level) else level[index]
            proof.append("R:sha256:" + sibling.hex())
        if len(level) % 2:
            level = level + [level[-1]]
        level = [hashlib.sha256(level[i] + level[i + 1]).digest() for i in range(0, len(level), 2)]
        index //= 2
    return tuple(proof)


def verify_merkle_proof(packet_hash: str, proof: Sequence[str], root: str) -> bool:
    """Recompute the ``merkle_proof`` path for ``packet_hash`` and compare with ``root``.

    ``proof`` must use the ``"L:sha256:<hex>"`` / ``"R:sha256:<hex>"`` encoding. The empty
    proof is valid only when ``root`` is exactly the leaf digest (a single-leaf epoch). Any
    malformed, flipped, or reordered element fails. An odd-carry self-pair is always emitted
    as ``R`` (sibling equal to the node); ``L`` with an equal sibling never comes from the
    tree and is rejected as tampered.
    """
    node = hashlib.sha256(packet_hash.encode("utf-8")).digest()
    for element in proof:
        if not isinstance(element, str):
            return False
        side, _, digest = element.partition(":")
        if side not in ("L", "R") or not digest.startswith("sha256:"):
            return False
        try:
            sibling = bytes.fromhex(digest.removeprefix("sha256:"))
        except ValueError:
            return False
        if len(sibling) != 32 or (side == "L" and sibling == node):
            return False
        node = hashlib.sha256(sibling + node if side == "L" else node + sibling).digest()
    return root == "sha256:" + node.hex()


def _epoch_id(content_hash: str) -> str:
    return "epc_" + sha256_hex(content_hash.encode("ascii"))[:16]


def _content_payload(epoch: Epoch) -> dict[str, Any]:
    payload: dict[str, Any] = epoch.model_dump(mode="json")
    for excludable in ("signature", "key_id", "epoch_id"):
        payload.pop(excludable)
    return payload


def epoch_content_hash(epoch: Epoch) -> str:
    """``sha256:``-prefixed canonical hash of the epoch.

    Excludes ``signature``, ``key_id``, and ``epoch_id``: the first two for
    signature-independence (ADR-0003), ``epoch_id`` because it is *derived* from this
    hash (same self-reference rule as the emptied ``hashes`` block in packet hashing).
    """
    return "sha256:" + sha256_hex(canonicalize(_content_payload(epoch)))


def create_epoch(
    *,
    tenant_id: str,
    watermark_event_seq: int,
    packet_hashes: tuple[str, ...],
    created_at: datetime,
    signer: Signer,
    prev_epoch_hash: str | None = None,
    meta: ReceiptMeta | None = None,
) -> Epoch:
    """Build and sign an epoch. Requires ``packet_hashes`` sorted and duplicate-free."""
    hashes = tuple(sorted(set(packet_hashes)))
    if hashes != tuple(packet_hashes):
        raise ValueError("packet_hashes must already be sorted and duplicate-free")
    unsigned = Epoch(
        epoch_id="",
        created_at=created_at,
        tenant_id=tenant_id,
        watermark_event_seq=watermark_event_seq,
        prev_epoch_hash=prev_epoch_hash,
        packet_hashes=hashes,
        merkle_root=merkle_root(hashes),
        packet_count=len(hashes),
        meta=meta,
    )
    content_hash = epoch_content_hash(unsigned)
    return unsigned.model_copy(
        update={
            "epoch_id": _epoch_id(content_hash),
            "key_id": signer.key_id,
            "signature": signer.sign(content_hash.encode("ascii")),
        }
    )


def verify_epoch(epoch: Epoch, public_key_bytes: bytes) -> bool:
    """Recompute the content hash + Merkle root and verify the Ed25519 signature."""
    if epoch.signature is None:
        return False
    if not epoch.packet_hashes:
        return False
    if epoch.key_id is not None and epoch.key_id != sha256_hex(public_key_bytes)[:16]:
        return False
    if epoch.merkle_root != merkle_root(epoch.packet_hashes):
        return False
    content_hash = epoch_content_hash(epoch)
    if epoch.epoch_id != _epoch_id(content_hash):
        return False
    return verify_signature(public_key_bytes.hex(), content_hash.encode("ascii"), epoch.signature)


def verify_chain(epochs: Sequence[Epoch], public_key_bytes: bytes) -> bool:
    """Each epoch verifies, ordering discipline holds, and prev links form the chain."""
    prev: Epoch | None = None
    for ep in epochs:
        if not verify_epoch(ep, public_key_bytes):
            return False
        if prev is None:
            if ep.prev_epoch_hash is not None:
                return False
        else:
            if ep.prev_epoch_hash != epoch_content_hash(prev):
                return False
            if ep.tenant_id != prev.tenant_id:
                return False
            if (ep.created_at.tzinfo is None) != (prev.created_at.tzinfo is None):
                return False  # mixed naive/aware timestamps cannot be ordered
            if ep.created_at < prev.created_at:
                return False
            if ep.watermark_event_seq < prev.watermark_event_seq:
                return False
        prev = ep
    return True


def verify_packet_in_epoch(epoch: Epoch, packet: ContextPacket) -> bool:
    """Membership check: recompute the packet hash and locate it under the epoch root.

    One root recomputation answers "does this epoch commit this packet?"; use
    ``merkle_proof`` / ``verify_merkle_proof`` for the compact O(log n) inclusion path.
    """
    recomputed = packet_content_hash(packet)
    if recomputed not in epoch.packet_hashes:
        return False
    return epoch.merkle_root == merkle_root(epoch.packet_hashes)


def _selftest() -> int:
    from kernel.router.compiler import compile_packet
    from protocol.models import Recipient, Scope

    as_of = datetime(2026, 1, 1, tzinfo=UTC)
    signer = Signer.from_seed_hex("00" * 32)
    scope = Scope(tenant_id="t1", project_id="p1", workflow_id="w1", task_id="task1")
    recipient = Recipient(agent_id="a1", role="implementer", stage="implement")

    def compile_one(objective: str) -> ContextPacket:
        return compile_packet(
            scope=scope,
            recipient=recipient,
            objective=objective,
            stage="implement",
            blocks=[],
            as_of=as_of,
            signer=signer,
        ).packet

    packet = compile_one("use Postgres for canonical storage")
    epoch = create_epoch(
        tenant_id="t1",
        watermark_event_seq=1,
        packet_hashes=(packet.hashes.content_hash,),
        created_at=as_of,
        signer=signer,
    )
    pub = bytes.fromhex(signer.public_hex)
    assert verify_epoch(epoch, pub)
    assert verify_packet_in_epoch(epoch, packet)
    assert verify_chain([epoch], pub)

    tampered = compile_one("use SQLite for canonical storage")
    assert not verify_packet_in_epoch(epoch, tampered), "tampered packet must not verify"
    assert not verify_epoch(epoch.model_copy(update={"tenant_id": "t2"}), pub)
    assert not verify_epoch(epoch, bytes.fromhex(Signer.from_seed_hex("01" * 32).public_hex))
    print("epoch selftest: OK (create -> verify -> tamper detected)")
    return 0


def _load_epoch(raw: Any) -> Epoch:
    if not isinstance(raw, dict):
        raise ValueError("epoch records must be JSON objects")
    return Epoch.model_validate(raw)


def _run_verify(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m protocol.epoch verify <epochs.json> --pubkey <hex>")
        return 1
    pubkey_hex = ""
    if "--pubkey" in argv:
        index = argv.index("--pubkey")
        if index + 1 < len(argv):
            pubkey_hex = argv[index + 1]
    if not pubkey_hex:
        print("FAIL: pass --pubkey <hex> (the out-of-band public key)")
        return 1
    try:
        raw = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
        epochs = [_load_epoch(item) for item in raw]
        public_key_bytes = bytes.fromhex(pubkey_hex)
    except Exception as exc:
        print(f"FAIL: {exc}")
        return 1

    expected_key_id = sha256_hex(public_key_bytes)[:16]
    if any(ep.key_id != expected_key_id for ep in epochs):
        print("FAIL: key_id mismatch between epoch and provided public key")
        return 1
    if not verify_chain(epochs, public_key_bytes):
        print("FAIL: epoch chain verification failed (tamper, reorder, or bad signature)")
        return 1
    print(f"OK: {len(epochs)} epoch(s) verified, key_id={expected_key_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["selftest"]:
        return _selftest()
    if args[:1] == ["verify"]:
        return _run_verify(args[1:])
    print("usage: python -m protocol.epoch selftest | verify <epochs.json> --pubkey <hex>")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
