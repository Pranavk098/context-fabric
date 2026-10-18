"""``cfx replay`` / ``cfx seal`` — make the replayable-receipt claim executable.

``cfx replay`` reloads a compile manifest from the store's ``compile_log``, verifies the
recorded blocks still match their hashes, and recompiles with the exact recorded parameters
(scope, recipient, objective, stage, ``as_of``, watermark, budget base; unsigned); the
resulting packet hash must equal the logged one. ``cfx seal`` commits the whole compile log
as one signed epoch (``protocol/epoch.py``) that ``cfx verify`` checks out-of-band.

Replay proves the context bytes handed to the agent — never the model's answer.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from kernel.router.compiler import compile_packet
from kernel.store import CompileManifest, SqliteStore
from protocol import epoch
from protocol.models import ContextBlock
from protocol.signing import Signer

_TOKENIZER_ID = "o200k_base"  # the tokenizer used for token estimates (kernel/router/packer.py)


def _open_store(store_path: str | Path, command: str) -> SqliteStore | None:
    try:
        return SqliteStore(store_path)
    except (sqlite3.Error, OSError) as exc:
        print(f"cfx {command}: cannot open store {store_path}: {exc}", file=sys.stderr)
        return None


def _recorded_blocks(
    store: SqliteStore, manifest: CompileManifest
) -> tuple[list[ContextBlock], list[str]]:
    """Store blocks named by the manifest, in store order (the compile-time input order).

    Blocks added after the manifest are ignored (not in ``block_ids``); recorded blocks
    whose content hash changed or that are gone produce drift lines.
    """
    wanted = dict(zip(manifest.block_ids, manifest.block_hashes, strict=False))
    blocks: list[ContextBlock] = []
    drifts: list[str] = []
    seen: set[str] = set()
    for block in store.load_blocks():
        expected = wanted.get(block.block_id)
        if expected is None:
            continue
        seen.add(block.block_id)
        if block.content_hash != expected:
            drifts.append(f"store drift: block {block.block_id} changed/missing")
        else:
            blocks.append(block)
    for block_id in manifest.block_ids:
        if block_id not in seen:
            drifts.append(f"store drift: block {block_id} changed/missing")
    return blocks, drifts


def replay(store_path: str | Path, packet_id: str) -> int:
    """Recompile from the logged manifest and compare packet content hashes.

    Exit codes: 0 ``REPLAY OK``; 1 no manifest, store drift, or hash mismatch;
    2 the store cannot be opened.
    """
    store = _open_store(store_path, "replay")
    if store is None:
        return 2
    manifest = store.get_compile(packet_id)
    if manifest is None:
        print(f"cfx replay: no compile manifest for packet {packet_id}", file=sys.stderr)
        return 1
    blocks, drifts = _recorded_blocks(store, manifest)
    if drifts:
        for line in drifts:
            print(line, file=sys.stderr)
        return 1
    result = compile_packet(
        scope=manifest.scope,
        recipient=manifest.recipient,
        objective=manifest.objective,
        stage=manifest.stage,
        blocks=blocks,
        watermark_event_seq=manifest.watermark_event_seq,
        budget_base=manifest.budget_base,
        as_of=manifest.as_of,
        purpose=manifest.purpose,
    )
    replayed_hash = result.packet.hashes.content_hash
    if replayed_hash != manifest.content_hash:
        print(f"REPLAY MISMATCH {manifest.content_hash} != {replayed_hash}")
        return 1
    print(f"REPLAY OK {packet_id} {replayed_hash}")
    return 0


def _load_epochs(out: Path) -> list[epoch.Epoch] | None:
    if not out.exists():
        return []
    try:
        raw = json.loads(out.read_text(encoding="utf-8"))
        return [epoch.Epoch.model_validate(item) for item in raw]
    except (OSError, ValueError, TypeError) as exc:
        print(f"cfx seal: cannot read existing {out}: {exc}", file=sys.stderr)
        return None


def seal(
    store_path: str | Path,
    private_key_hex: str,
    out_path: str | Path,
    *,
    model: str = "",
    engine: str = "",
) -> int:
    """Commit the whole compile log as one signed epoch, appending to ``out_path``.

    ``packet_hashes`` = the logged content hashes; ``watermark_event_seq`` = the maximum
    recorded watermark; ``created_at`` = the latest recorded manifest time (so re-sealing
    the same log is deterministic). Re-sealing with nothing new is a no-op; a log spanning
    multiple tenants is refused (verification enforces a single tenant per chain).

    Exit codes: 0 sealed or already covered; 1 empty log, mixed tenants, or unreadable
    epochs file; 2 bad key or unreadable store.
    """
    try:
        signer = Signer.from_seed_hex(private_key_hex)
    except (TypeError, ValueError) as exc:
        print(f"cfx seal: --private-key must be 32-byte hex: {exc}", file=sys.stderr)
        return 2
    store = _open_store(store_path, "seal")
    if store is None:
        return 2
    manifests = store.load_compile_log(limit=None)
    if not manifests:
        print(
            "cfx seal: compile log is empty: nothing to seal"
            " (serve with a .db fabric to record compiles)",
            file=sys.stderr,
        )
        return 1
    tenants = {manifest.scope.tenant_id for manifest in manifests}
    if len(tenants) > 1:
        print(
            f"cfx seal: compile log spans {len(tenants)} tenants; seal per store", file=sys.stderr
        )
        return 1
    out = Path(out_path)
    epochs = _load_epochs(out)
    if epochs is None:
        return 1
    hashes = tuple(sorted({manifest.content_hash for manifest in manifests}))
    if epochs and epochs[-1].merkle_root == epoch.merkle_root(hashes):
        print(f"cfx seal: nothing new: epoch {epochs[-1].epoch_id} already covers the log")
        return 0
    sealed = epoch.create_epoch(
        tenant_id=tenants.pop(),
        watermark_event_seq=max(manifest.watermark_event_seq for manifest in manifests),
        packet_hashes=hashes,
        created_at=max(manifest.created_at for manifest in manifests),
        signer=signer,
        prev_epoch_hash=epoch.epoch_content_hash(epochs[-1]) if epochs else None,
        meta=epoch.ReceiptMeta(model_id=model, tokenizer_id=_TOKENIZER_ID, engine=engine),
    )
    payload = [e.model_dump(mode="json") for e in epochs] + [sealed.model_dump(mode="json")]
    try:
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"cfx seal: cannot write {out}: {exc}", file=sys.stderr)
        return 1
    print(f"epoch_id: {sealed.epoch_id}")
    print(f"packets: {sealed.packet_count}")
    return 0
