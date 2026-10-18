"""``cfx fold`` / ``cfx fork`` — the packet-level MMU on the product surface.

``cfx fold`` folds raw tool-output events in a SQLite store into Tier-1 fact blocks and
appends them back (append-only; raw events are never evicted). The selection rule for
"raw events" is ``block_kind in {observation, artifact}`` AND ``fidelity == raw`` AND
``content is not None`` AND ``lifecycle_status == routable``, in store order: already
admitted raw tool outputs only. Draft/quarantined blocks are left to the policy gate
(``fold_raw_events`` is policy-agnostic; CD-18), and ``ref_uri`` pointers are not raw
content. The selected sequence is cut into consecutive micro-epochs of ``k`` (default 3):
complete epochs mint facts, the incomplete tail stays raw. Folding is idempotent: a
fact's ``block_id`` derives from its source event's id and content hash, so a re-fold
mints byte-identical blocks and the store dedupes them by ``block_id``.

``cfx fork`` reloads a parent compile manifest, rebuilds the parent packet from its
recorded block set with the exact drift checks of ``cfx replay``, and spawns a COW child
(``kernel.mmu.cow_forker.fork_packet``) for a new agent/role/objective. ``as_of`` is the
parent manifest's ``as_of`` — never the clock — so identical invocations emit
byte-identical child JSON.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from kernel.mmu.cow_forker import fork_packet
from kernel.mmu.epoch_folder import DEFAULT_K, FOLDABLE_KINDS, fold_raw_events
from kernel.router.compiler import compile_packet
from kernel.store import SqliteStore, StoreConflict
from protocol.models import (
    BlockKind,
    ContextBlock,
    Fidelity,
    LifecycleStatus,
    Recipient,
)
from sidecar.replay import _recorded_blocks


def is_raw_event(block: ContextBlock) -> bool:
    """The ``cfx fold`` selection rule: an admitted raw tool-output block.

    ``observation``/``artifact`` kind, ``fidelity=raw``, carries content, and is
    ``routable`` (drafts are quarantined and must not be folded; CD-18).
    """
    return (
        block.block_kind in FOLDABLE_KINDS
        and block.fidelity is Fidelity.raw
        and block.content is not None
        and block.lifecycle_status is LifecycleStatus.routable
    )


def _open(store_path: str | Path, command: str) -> SqliteStore | None:
    if not Path(store_path).is_file():
        print(f"cfx {command}: store not found: {store_path}", file=sys.stderr)
        return None
    try:
        return SqliteStore(store_path)
    except (sqlite3.Error, OSError) as exc:
        print(f"cfx {command}: cannot open store {store_path}: {exc}", file=sys.stderr)
        return None


def fold(store_path: str | Path, *, k: int = DEFAULT_K) -> int:
    """Fold the store's raw events and append the minted facts (append-only).

    Prints ``folded: <n>``, ``appended: <n>``, ``deduped: <n>``. Idempotent: re-running
    mints identical facts, which ``SqliteStore.append_many`` dedupes by ``block_id``.

    Exit codes: 0 ok; 1 store error; 2 usage (``k < 1``).
    """
    if k < 1:
        print("cfx fold: --k must be >= 1", file=sys.stderr)
        return 2
    store = _open(store_path, "fold")
    if store is None:
        return 1
    try:
        blocks = store.load_blocks()
    except (ValueError, sqlite3.Error) as exc:
        print(f"cfx fold: cannot read store {store_path}: {exc}", file=sys.stderr)
        return 1
    result = fold_raw_events([b for b in blocks if is_raw_event(b)], k=k)
    facts = [b for b in result.ledger if b.block_kind is BlockKind.fact]
    try:
        appended, deduped = store.append_many(facts)
    except StoreConflict as exc:
        print(f"cfx fold: {exc}", file=sys.stderr)
        return 1
    print(f"folded: {result.folded}")
    print(f"appended: {appended}")
    print(f"deduped: {deduped}")
    return 0


def fork(
    store_path: str | Path,
    packet_id: str,
    *,
    agent: str,
    role: str,
    objective: str,
    stage: str | None = None,
    out_path: str | Path | None = None,
) -> int:
    """Spawn a COW child from the parent receipt logged for ``packet_id``.

    The parent packet is rebuilt from the manifest's recorded block set with the same
    filtering and drift checks as ``cfx replay``; store drift or a parent hash mismatch
    exits 1. ``stage`` defaults to the parent manifest's stage. ``as_of`` is the
    manifest's ``as_of``, so identical invocations emit byte-identical JSON. The child
    packet JSON goes to stdout; ``child_packet_id``/``parent_packet_id`` go to stderr.

    Exit codes: 0 ok; 1 store/no-manifest/drift/write error; 2 usage.
    """
    if not (agent.strip() and role.strip() and objective.strip()):
        print("cfx fork: --agent, --role and --objective must be non-empty", file=sys.stderr)
        return 2
    store = _open(store_path, "fork")
    if store is None:
        return 1
    try:
        manifest = store.get_compile(packet_id)
        if manifest is None:
            print(f"cfx fork: no compile manifest for packet {packet_id}", file=sys.stderr)
            return 1
        blocks, drifts = _recorded_blocks(store, manifest)
    except (ValueError, sqlite3.Error) as exc:
        print(f"cfx fork: cannot read store {store_path}: {exc}", file=sys.stderr)
        return 1
    if drifts:
        for line in drifts:
            print(line, file=sys.stderr)
        return 1
    parent = compile_packet(
        scope=manifest.scope,
        recipient=manifest.recipient,
        objective=manifest.objective,
        stage=manifest.stage,
        blocks=blocks,
        watermark_event_seq=manifest.watermark_event_seq,
        budget_base=manifest.budget_base,
        as_of=manifest.as_of,
    ).packet
    if parent.hashes.content_hash != manifest.content_hash:
        print(
            f"FORK MISMATCH {manifest.content_hash} != {parent.hashes.content_hash}"
            " (recorded parent does not reproduce)",
            file=sys.stderr,
        )
        return 1
    child_stage = manifest.stage if stage is None else stage
    child = fork_packet(
        parent,
        recipient=Recipient(agent_id=agent, role=role, stage=child_stage),
        objective=objective,
        stage=child_stage,
        as_of=manifest.as_of,
    )
    payload = json.dumps(child.model_dump(mode="json"), indent=2)
    if out_path is not None:
        try:
            Path(out_path).write_text(payload + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"cfx fork: cannot write {out_path}: {exc}", file=sys.stderr)
            return 1
    print(payload)
    print(f"child_packet_id: {child.packet_id}", file=sys.stderr)
    print(f"parent_packet_id: {child.parent_packet_id}", file=sys.stderr)
    return 0
