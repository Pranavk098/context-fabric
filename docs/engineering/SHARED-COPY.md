# The Shared Copy — copy-on-write memory states (v0)

## What this is

Agents that work in parallel need to share what they know without corrupting each other's
view. `kernel/epochs.py` provides the v0 primitive: a **`MemoryState`** is a frozen snapshot
over immutable `ContextBlock` objects; `cow_fork` gives a child the *same* block objects
(no copy); child edits (`child_add`, `child_remove`) return new child states and never touch
the parent; **`epoch_fold`** is the only merge point, and it rejects conflicts instead of
silently resolving them. State ids are deterministic (sha256 over sorted block content
hashes + the fork point), so lineage is reproducible and auditable.

## Why it matters (and what it is not)

The fork/CoW *mechanism* is already commodity — vLLM prefix caching, LMCache P2P, and ForkKV
all ship sharing (`docs/research/SHARED-MEMORY-PRIOR-ART.md` §1–§2). The unclaimed part is
the **consistency contract + ownership semantics + deterministic lineage**: the 2026 KV
survey notes shared caches "rarely state a consistency contract," and Letta documents
concurrent shared-block writes as an anti-pattern that loses updates. This module is the
contract, not a new KV engine. It does not touch GPU memory, engines, or model outputs.

## The consistency contract

- **Blocks are immutable.** `ContextBlock` is frozen; a shared block can never be mutated
  after the fact, so sharing it is safe by construction.
- **Fork is reference-share.** `cow_fork(parent)` returns a child whose `blocks` are the
  *same objects* as the parent's, with `parent_state_id = parent.state_id`. O(1) block
  copies — zero.
- **Edits are private.** `child_add` / `child_remove` return a *new* state; the parent (and
  any sibling) is untouched and keeps its original `state_id`.
- **Fold is explicit.** `epoch_fold(child, parent)` is the only path that merges shared
  state, and only for an immediate descendant (`child.parent_state_id == parent.state_id`,
  else `FoldConflict("not-a-descendant")`).
- **Conflicts are rejected, never silently merged.** Same `block_id` with a different
  `content_hash` anywhere in the fold → `FoldConflict` with a `.detail` naming the block and
  both hashes. Identical `(block_id, content_hash)` pairs are deduped.
- **Fold is add-only in v0.** A child's removals do not propagate; the parent keeps blocks a
  child tombstoned. Fold never deletes.
- **Lineage is deterministic.** `state_id = sha256_hex(canonicalize(sorted(content_hashes) +
  [parent_state_id or ""]))`. Same ops → same id; different content or fork point → different
  id; repeated folds → identical state ids and counts.
- **Watermarks propagate upward.** The folded state's `watermark_event_seq` is the max of
  parent and child; the child inherits the parent's watermark at fork.

## Claims we may make

> Context Fabric's shared copy is an explicit consistency contract over shared memory:
> immutable blocks, reference-shared forks, private edits, and a validated add-only fold
> that rejects conflicts and produces deterministic, auditable state lineage.

**We may claim:** the contract, deterministic packet/state identity, and auditable lineage
(state ids chain by fork point).

**We may not claim:** CoW-fork or cross-agent-sharing novelty (ForkKV, LMCache P2P, vLLM APC
own it); reproducibility or determinism of model *outputs* (only of compiled packets and
epoch states — `meta.note = "output_reproduction_out_of_scope"`); any vendor benchmark
numbers. Research caveats hold verbatim: prefix caching and selective reuse change agent
trajectories (69.0% answer variation in `2610.05833`; 37.4% trajectory divergence in
`2609.04748`), and multi-party memory is unsolved (GroupMemBench best ~46%, `2605.14498`).

## Usage

```python
from kernel.epochs import child_add, cow_fork, epoch_fold, new_state

shared = new_state([block_a, block_b], watermark_event_seq=7)  # root snapshot
child = cow_fork(shared)  # shared, no copies
child = child_add(child, block_c)  # private to child
folded = epoch_fold(child, shared)  # validate + merge
assert (folded.added, folded.deduped) == (1, 2)
shared.state_id == folded.state.state_id  # add-only, no surprises
```

```powershell
uv run python -m kernel.epochs   # self-check: fork -> private edit -> isolation -> fold
```

## v0 limits

- **Add-only fold:** removals stay private to the child; there is no merge of tombstones.
- **Immediate-descendant folds only:** siblings cannot be merged into each other; fold
  `child` into its fork `parent`, then fork again from the folded state.
- **Implicit tombstones:** a removed block is simply absent from the child's `blocks`; an id
  removed by the child may be re-added with new content (that is the edit path).
- **No locking or leases:** one writer per child is assumed. Leases/version vectors and
  signed epoch manifests embedding state ids are the documented next steps (§6 of
  `SHARED-MEMORY-PRIOR-ART.md`).
