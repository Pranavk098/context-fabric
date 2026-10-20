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
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from kernel.router.compiler import compile_packet
from kernel.store import CompileManifest, SqliteStore
from protocol import epoch
from protocol.canonical import canonicalize, sha256_hex
from protocol.models import ContextBlock
from protocol.signing import Signer
from sidecar.blocks import stored_block
from sidecar.obsfold import make_stub

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


def _folded_from_events(
    messages: list[dict[str, Any]], events: list[Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Reconstruct the folded message list from the recorded unfoldeds + fold events.

    Each fold event names a message index whose content was replaced by a stub; the stub is
    regenerated deterministically (``obsfold.make_stub``) and the event's ``content_hash``
    must match the message content it folds, so tampering with either is caught before the
    ``seen_hash`` comparison in ``replay_fold``.
    """
    folded = [dict(message) for message in messages]
    drifts: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            drifts.append("malformed fold event")
            continue
        index = event.get("message_index")
        if not isinstance(index, int) or not 0 <= index < len(folded):
            drifts.append(f"fold event index {index!r} out of range")
            continue
        content = folded[index].get("content")
        if not isinstance(content, str):
            drifts.append(f"message {index} has non-string content")
            continue
        if event.get("content_hash") != "sha256:" + sha256_hex(content.encode("utf-8")):
            drifts.append(f"message {index} content hash changed")
            continue
        stub, stub_id = make_stub(content, str(event.get("tool_name") or "tool"))
        if stub_id != event.get("stub_id"):
            drifts.append(f"message {index} stub id drifted")
        folded[index]["content"] = stub
    return folded, drifts


def _verify_expansion(
    store: SqliteStore, messages: list[dict[str, Any]], events: list[Any]
) -> list[str]:
    """Each folded observation must expand losslessly from the ``ctx://`` store ``obs_`` blocks."""
    drifts: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue  # already reported by _folded_from_events
        index = event.get("message_index")
        stub_id = event.get("stub_id")
        if not isinstance(index, int) or not 0 <= index < len(messages):
            continue  # already reported by _folded_from_events
        if not isinstance(stub_id, str):
            drifts.append("fold event missing stub id")
            continue
        block = stored_block(store, "obs_" + stub_id)
        if block is None or block.content_hash != event.get("content_hash"):
            drifts.append(f"stub {stub_id} missing/changed in the ctx:// store")
            continue
        original = messages[index].get("content")
        if isinstance(original, str) and block.content != original:
            drifts.append(f"stub {stub_id} expansion is not lossless")
    return drifts


def fold_replay_bodies(
    store: SqliteStore, session_id: str | None = None, *, expand: bool = False
) -> tuple[list[bytes], list[str]]:
    """Reconstruct every recorded folded request: ``(bodies, drift lines)``.

    Each body is the byte-exact request the sidecar forwarded upstream — the recorded
    unfolded request with fold events applied — or, with ``expand``, the original unfolded
    request. Drift lines describe any hash mismatch against the recorded receipts.
    """
    bodies: list[bytes] = []
    drifts: list[str] = []
    for request in store.load_fold_requests(session_id):
        seq = request["request_seq"]
        payload = request["request"]
        if not isinstance(payload, dict):
            drifts.append(f"request {seq}: recorded request is not an object")
            bodies.append(b"")
            continue
        messages = payload.get("messages")
        if not isinstance(messages, list) or not all(
            isinstance(message, dict) for message in messages
        ):
            drifts.append(f"request {seq}: recorded request has no message list")
            bodies.append(b"")
            continue
        events = request["events"] if isinstance(request["events"], list) else []
        folded, fold_drifts = _folded_from_events(messages, events)
        drifts.extend(fold_drifts)
        if not fold_drifts and "sha256:" + sha256_hex(canonicalize(folded)) != request["seen_hash"]:
            drifts.append(f"request {seq}: seen hash mismatch")
        if expand:
            drifts.extend(_verify_expansion(store, messages, events))
        out = dict(payload)
        out["messages"] = messages if expand else folded
        bodies.append(json.dumps(out).encode("utf-8"))
    return bodies, drifts


def replay_fold(
    store_path: str | Path, session_id: str | None = None, *, expand: bool = False
) -> int:
    """Replay every recorded folded request and verify the recorded receipts.

    Exit codes: 0 every reconstructed request matches its ``seen_hash`` (and, with
    ``--expand``, its stored ``obs_`` content); 1 no recorded requests, or drift/mismatch;
    2 the store cannot be opened.
    """
    store = _open_store(store_path, "replay-fold")
    if store is None:
        return 2
    requests = store.load_fold_requests(session_id)
    if not requests:
        print("cfx replay-fold: no folded requests recorded in the store", file=sys.stderr)
        return 1
    bodies, drifts = fold_replay_bodies(store, session_id, expand=expand)
    if drifts:
        for line in drifts:
            print(f"cfx replay-fold: {line}", file=sys.stderr)
        return 1
    for request, body in zip(requests, bodies, strict=True):
        if expand:
            sys.stdout.write(body.decode("utf-8") + "\n")
        else:
            print(f"FOLD REPLAY OK {request['request_seq']} {request['seen_hash']}")
    return 0


def _read_seed_hex(key_file: str | Path | None) -> str:
    """The Ed25519 seed hex from ``key_file`` or the ``CF_SEAL_KEY`` env var.

    Never argv: a seed on the command line is visible in ``ps`` / shell history
    (audit AU2-M2).
    """
    if key_file is not None:
        try:
            return Path(key_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"cannot read key file {key_file}: {exc}") from exc
    seed = os.environ.get("CF_SEAL_KEY", "").strip()
    if not seed:
        raise ValueError("set CF_SEAL_KEY (32-byte hex) or pass --key-file <path>")
    return seed


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
    out_path: str | Path,
    *,
    key_file: str | Path | None = None,
    model: str = "",
    engine: str = "",
) -> int:
    """Commit the whole compile log as one signed epoch, appending to ``out_path``.

    The signing seed is read from ``key_file`` (a file containing the 32-byte hex) or the
    ``CF_SEAL_KEY`` environment variable — never from argv (audit AU2-M2).

    ``packet_hashes`` = the logged content hashes; ``watermark_event_seq`` = the maximum
    recorded watermark; ``created_at`` = the latest recorded manifest time (so re-sealing
    the same log is deterministic). Re-sealing with nothing new is a no-op; a log spanning
    multiple tenants is refused (verification enforces a single tenant per chain).

    Exit codes: 0 sealed or already covered; 1 empty log, mixed tenants, or unreadable
    epochs file; 2 bad key or unreadable store.
    """
    try:
        seed_hex = _read_seed_hex(key_file)
    except ValueError as exc:
        print(f"cfx seal: {exc}", file=sys.stderr)
        return 2
    try:
        signer = Signer.from_seed_hex(seed_hex)
    except (TypeError, ValueError) as exc:
        print(f"cfx seal: seed must be 32-byte hex: {exc}", file=sys.stderr)
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
