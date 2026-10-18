# Packet-Level MMU: `EPOCH_FOLD` and `COW_FORK`

**Status:** v0 implemented at `kernel/mmu/` (`epoch_folder.py`, `cow_forker.py`). Not yet wired
into the sidecar compile path. Tests: `tests/test_mmu.py`.

## What and why

Two packet-level syscalls from the Phase 2 design close the context window loop:

* **`EPOCH_FOLD`** — intra-turn tool loops append raw outputs to Tier 3 for `k` steps, then
  fold each completed micro-epoch: large raw outputs become reversible Tier 2 `ctx://`
  pointers plus compact Tier 1 facts. The window stops growing; nothing is lost.
* **`COW_FORK`** — when an agent spawns a sub-agent, the child mounts the parent's
  immutable Tier 0/1/2 pages unchanged and gets a brand-new Tier 3. Tier 0 is the shared
  rendered head, so the child reuses the parent's KV-cache prefix byte-for-byte there;
  Tier 1/2 are identical block sets but render after the per-agent instruction.

## Spec quotes

* `docs/design/Build-Plan-Validation-Gate.md:44` — "Implement `EPOCH_FOLD` (k=3 steps
  append-only -> fold raw tool outputs into Tier 1 facts + Tier 2 `ctx://` pointers) and
  `COW_FORK` (sub-agent spawning with constant window size)."
* `docs/design/Arch-Spec.md` invariant 5 — "Intra-turn tool loops MUST append strictly to
  Tier 3 for $k$ steps before triggering an `EPOCH_FOLD`, preventing mid-turn causal
  KV-cache invalidation."
* `docs/design/Arch-Spec.md` invariant 6 — "Evicted raw tool outputs and large artifacts
  MUST be replaced with addressable `ctx://` swap pointers in Tier 2."
* `docs/design/Arch-Spec.md` invariant 7 — "Child agents MUST share immutable Tier 0 and
  Tier 1 pages with parent agents and discard their ephemeral Tier 3 scratchpad."
* `docs/design/Module-Implementation.md` §D.1 — "Take all tool calls and tool outputs from
  steps `1..k` in Tier 3 ... extract the key structured result into a compact `fact` block
  ... Create a `SwapHandle` (`ctx://obs/...`) in Tier 2 pointing to the raw tool output ...
  Append the newly minted `fact` ... blocks to the end of **Tier 1**, clear the raw tool
  outputs from **Tier 3**, increment `epoch_number`."
* `docs/design/Module-Implementation.md` §D.2 — "Initialize Child Packet `C` with the exact
  same `tier0_cartridge_ref` and `tier1_ledger_blocks` ... Allocate a clean, empty Tier 3
  `working_register`."

## `EPOCH_FOLD` semantics (`kernel/mmu/epoch_folder.py`)

`fold_raw_events(events, *, k=3, fact_tokens=40, fold_threshold_tokens=60) -> FoldResult`

1. The input is a sequence of raw event blocks in append order. Only `observation` and
   `artifact` blocks are foldable — they are the "raw tool outputs". Any other kind passes
   through untouched.
2. The sequence is cut into consecutive micro-epochs of `k` events. A **complete**
   micro-epoch folds; the trailing remainder (`len(events) % k`) stays raw (the append-only
   k-step gate: fewer than `k` accumulated steps trigger no fold).
3. Inside a complete micro-epoch, each foldable event with
   `token_estimate > fold_threshold_tokens` (strictly above) mints:
    * a Tier 1 **fact** block — `" ".join(content.split()[:fact_tokens]) + " (folded from
      <block_id>)"`, `fidelity=extract`, `creator=tool`, `source_event_seq` preserved,
      `trust_domain`/`classification`/RBAC fields and `lifecycle_status` preserved (a
      draft stays a draft, so folding cannot promote it past the policy gate),
      `content_hash` and `token_estimate` recomputed; and
   * a Tier 2 **`SwapHandle`** — `uri="ctx://obs/<block_id>"`, summary = the same
     `fact_tokens` truncation, `tokens_raw` = the raw block's estimate, `sha256` over the
     full raw content.
4. Events at or below the threshold, and non-foldable kinds, are emitted unchanged.
5. `FoldResult.ledger` is the replacement sequence in input order; the caller routes by
   `memory_tier` (facts to Tier 1, survivors stay where they were). `folded + kept_raw ==
   len(events)`.

```python
from kernel.mmu import fold_raw_events

result = fold_raw_events(packet.tiers.tier3.observations, k=3)
new_tier1 = packet.tiers.tier1.blocks + tuple(
    b for b in result.ledger if b.memory_tier is MemoryTier.ledger
)
new_tier2 = packet.tiers.tier2.handles + result.handles
new_tier3 = tuple(b for b in result.ledger if b.memory_tier is MemoryTier.register)
```

## `COW_FORK` semantics (`kernel/mmu/cow_forker.py`)

`fork_packet(parent, *, recipient, objective, stage, as_of, budget_base=None, signer=None)
-> ContextPacket`

* **Shared by reference:** `tier0`, `tier1` and `tier2` are the exact same model objects on
  the child (pydantic frozen models — the parent can never be mutated through the child).
* **Fresh Tier 3:** a new instruction for the child's objective/stage/role; no inherited
  probes, observations, or recency anchor.
* **Lineage (ADR-0005):** `parent_packet_id=parent.packet_id`, `epoch_number + 1`,
  `spawn_depth + 1`; `scope`, `watermark_event_seq`, `conflict_alerts` and `budget` are kept
  (`budget_base` overrides the budget via `TokenBudget.from_base`).
* **Deterministic ids:** `packet_id`/`route_id` reuse the compiler's `_det_id` convention
  and include `parent.packet_id` and the child epoch, so identical forks are identical and
  different parents/objectives diverge. `hashes.content_hash` is recomputed with
  `packet_content_hash`; the child is re-signed only when a `Signer` is passed.
* **Constant window size:** the child's `total_tokens` is the parent total minus the
  discarded Tier 3 plus the fresh instruction, checked against the budget (the parent's
  budget unless overridden), and `ContextPacket` rejects any total above
  `hard_total_tokens`. It shrinks whenever the parent's Tier 3 is larger than the fresh
  instruction; a much longer child objective can make it grow, but never past the hard
  budget.

```python
from kernel.mmu import fork_packet

child = fork_packet(
    parent,
    recipient=Recipient(agent_id="aud", role="auditor", stage="audit"),
    objective="audit the storage change",
    stage="audit",
    as_of=AS_OF,
    signer=signer,  # optional
)
```

## Interpretations and divergences

* **Handle URI is `ctx://obs/<block_id>`**, per `Module-Implementation.md:84`
  (`ctx://obs/...`). The router packer mints `ctx://block/<block_id>` for its own overflow
  handles; both resolve to the stored block. A shared resolver should normalize both when
  `PAGE_FAULT_EXPAND` is wired.
* **Tier 2 is shared on fork** (the spec names only Tier 0/1), so the child inherits the
  parent's swap table and its `ctx://` handles stay resolvable. This does **not** extend
  the cacheable rendered prefix: with Tier 0 first and the per-agent instruction second
  (`sidecar/render.py`), only Tier 0 is byte-identical across agents; Tier 1/2 render
  after the instruction. The KV-reuse claim is therefore Tier-0 only, not "Tier 0+1+2".
* **Tier 1 is shared unfiltered (known gap).** `Module-Implementation.md` §D.2 says to
  filter the parent ledger when the child has stricter `denied_roles` (e.g. spawning an
  auditor). v0 reuses the ledger object without re-running `apply_policy`, so a child
  whose role is denied a parent-visible block still receives it. Fork across trust
  boundaries only after adding that policy pass (and recomputing `total_tokens`).
* **The fold threshold is per event**, while §D.1 names a packet-level Tier 3 token
  trigger. v0 keeps the pure function small; the packet trigger belongs in the sidecar
  adapter that owns Tier 3 token accounting.
* **`observed_at`/`valid_from` are copied from the source event**, never read from the
  clock, so repeated folds are byte-identical.
* **`kernel/epochs.py` is a different layer.** It implements the block-store consistency
  contract (`MemoryState`, O(1) fork, explicit merge). `kernel/mmu/` is the packet-level
  algorithm from Module D. The two do not call each other.

## Determinism

No clock, no RNG, no set/dict iteration in emitted order. Input order is authoritative,
ids are `_det_id`/sha256-derived, and every produced block round-trips through
`ContextBlock.model_validate(model_dump())`. Two identical runs produce identical
`FoldResult` and identical child packets (tested).

## v0 limits (honest)

* **Not wired into the sidecar compile path** — nothing in `sidecar/` calls these yet; they
  are a kernel library with tests.
* The §D.1 failed-tool branch (`negative_knowledge` on non-zero exit / test failure) is not
  implemented: `ContextBlock` carries no tool-result metadata contract to key on.
* No persistence, no `PAGE_FAULT_EXPAND` resolver, no signed epoch manifests; fork ids are
  reproducible (not secret), so in-process trust only.
