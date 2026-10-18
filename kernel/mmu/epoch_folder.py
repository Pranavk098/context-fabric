"""Packet-level EPOCH_FOLD — k-step append-only folding of raw tool outputs.

Spec: ``docs/design/Build-Plan-Validation-Gate.md:44`` — "Implement ``EPOCH_FOLD`` (k=3
steps append-only -> fold raw tool outputs into Tier 1 facts + Tier 2 ``ctx://``
pointers)"; the packet-level algorithm in ``docs/design/Module-Implementation.md``
§D.1 (lines 78-85); invariant 5 of ``docs/design/Arch-Spec.md`` — "Intra-turn tool loops
MUST append strictly to Tier 3 for $k$ steps before triggering an ``EPOCH_FOLD``".

Semantics (v0):
* Input is a sequence of raw event blocks in append order. Only ``observation`` and
  ``artifact`` blocks are foldable (they are the "raw tool outputs"); every other kind
  passes through untouched.
* The sequence is cut into consecutive micro-epochs of ``k`` events (default 3). Inside a
  complete micro-epoch, each foldable event with ``token_estimate > fold_threshold_tokens``
  mints a Tier 1 ``fact`` block (deterministic whitespace truncation of the raw content,
  ``fidelity='extract'``) plus a Tier 2 ``SwapHandle`` at ``ctx://obs/<block_id>``. Events
  at or below the threshold, non-foldable kinds, and the incomplete tail (fewer than ``k``
  trailing events) are emitted unchanged — append-only, never lossy.
* Fully deterministic: no clock, no RNG, input order preserved; timestamps and policy
  fields are copied from the source event and hashes are recomputed exactly like the
  compiler does for synthesized blocks (``_det_id`` / ``sha256:`` + ``sha256_hex``).

v0 gap: the failed-tool branch of §D.1 step 2 (mint ``negative_knowledge`` on non-zero
exit/test failure) needs a tool-result metadata contract that ``ContextBlock`` does not
carry yet; this folder mints facts only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from kernel.cartridge.serializer import count_tokens
from kernel.router.compiler import _det_id
from protocol.canonical import sha256_hex
from protocol.models import (
    BlockKind,
    ContextBlock,
    Creator,
    CreatorType,
    Fidelity,
    MemoryTier,
    SwapHandle,
)

DEFAULT_K = 3
DEFAULT_FACT_TOKENS = 40
DEFAULT_FOLD_THRESHOLD_TOKENS = 60
FOLDABLE_KINDS: frozenset[BlockKind] = frozenset({BlockKind.observation, BlockKind.artifact})
SWAP_URI_PREFIX = "ctx://obs/"


@dataclass(frozen=True)
class FoldResult:
    """Outcome of one or more micro-epoch folds.

    ``ledger`` is the replacement block sequence (minted facts in place of folded raw
    events, everything else unchanged, input order preserved); the caller routes by
    ``memory_tier`` (facts append to Tier 1, survivors keep their original tier).
    ``folded`` and ``kept_raw`` partition the input: ``folded + kept_raw == len(events)``.
    """

    ledger: tuple[ContextBlock, ...]
    handles: tuple[SwapHandle, ...]
    folded: int
    kept_raw: int


def _truncate(content: str, fact_tokens: int) -> str:
    return " ".join(content.split()[:fact_tokens])


def _fold_fact(event: ContextBlock, *, fact_tokens: int) -> ContextBlock:
    content = f"{_truncate(event.content or '', fact_tokens)} (folded from {event.block_id})"
    return ContextBlock(
        block_id=_det_id("blk_fold", event.block_id, event.content_hash),
        block_kind=BlockKind.fact,
        memory_tier=MemoryTier.ledger,
        media_type=event.media_type,
        content=content,
        content_hash="sha256:" + sha256_hex(content.encode("utf-8")),
        token_estimate=count_tokens(content),
        source_event_seq=event.source_event_seq,
        creator=Creator(type=CreatorType.tool, id=event.creator.id),
        fidelity=Fidelity.extract,
        lifecycle_status=event.lifecycle_status,
        observed_at=event.observed_at,
        valid_from=event.valid_from,
        valid_to=event.valid_to,
        classification=event.classification,
        trust_domain=event.trust_domain,
        allowed_roles=event.allowed_roles,
        denied_roles=event.denied_roles,
        allowed_purposes=event.allowed_purposes,
    )


def _handle(event: ContextBlock, *, fact_tokens: int) -> SwapHandle:
    content = event.content or ""
    return SwapHandle(
        uri=f"{SWAP_URI_PREFIX}{event.block_id}",
        summary=_truncate(content, fact_tokens),
        tokens_raw=event.token_estimate,
        sha256="sha256:" + sha256_hex(content.encode("utf-8")),
    )


def fold_raw_events(
    events: Sequence[ContextBlock],
    *,
    k: int = DEFAULT_K,
    fact_tokens: int = DEFAULT_FACT_TOKENS,
    fold_threshold_tokens: int = DEFAULT_FOLD_THRESHOLD_TOKENS,
) -> FoldResult:
    """Fold complete k-event micro-epochs of raw tool output into facts + swap handles.

    Returns the replacement ``ledger`` sequence (input order preserved), the Tier 2
    ``handles``, and the ``folded``/``kept_raw`` counts. Deterministic for identical
    inputs; the raw content is never mutated, and eviction stays reversible via the
    ``ctx://obs/<block_id>`` pointer.
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    foldable_cut = (len(events) // k) * k
    ledger: list[ContextBlock] = []
    handles: list[SwapHandle] = []
    folded = kept_raw = 0
    for index, event in enumerate(events):
        if (
            index < foldable_cut
            and event.block_kind in FOLDABLE_KINDS
            and event.content is not None
            and event.token_estimate > fold_threshold_tokens
        ):
            ledger.append(_fold_fact(event, fact_tokens=fact_tokens))
            handles.append(_handle(event, fact_tokens=fact_tokens))
            folded += 1
        else:
            ledger.append(event)
            kept_raw += 1
    return FoldResult(tuple(ledger), tuple(handles), folded, kept_raw)


if __name__ == "__main__":
    from datetime import UTC, datetime

    def raw(bid: str, seq: int, content: str, tokens: int) -> ContextBlock:
        return ContextBlock(
            block_id=bid,
            block_kind=BlockKind.observation,
            memory_tier=MemoryTier.register,
            content=content,
            content_hash="sha256:" + sha256_hex(content.encode("utf-8")),
            token_estimate=tokens,
            source_event_seq=seq,
            creator=Creator(type=CreatorType.tool, id="bash"),
            fidelity=Fidelity.raw,
            trust_domain="t1",
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        )

    big_a = raw("obs_a", 1, " ".join(f"a{i}" for i in range(100)), 100)
    small = raw("obs_b", 2, "tiny", 5)
    big_c = raw("obs_c", 3, " ".join(f"c{i}" for i in range(100)), 100)
    tail = raw("obs_d", 4, " ".join(f"d{i}" for i in range(100)), 100)
    result = fold_raw_events([big_a, small, big_c, tail], k=3)
    assert result.folded == 2 and result.kept_raw == 2
    assert [b.block_id for b in result.ledger][1] == "obs_b"
    assert result.ledger[3] is tail, "incomplete tail must stay raw (k-step gate)"
    fact = result.ledger[0]
    fact_content = fact.content
    assert fact_content is not None and fact_content.endswith("(folded from obs_a)")
    assert fact.token_estimate == count_tokens(fact_content)
    assert result.handles[0].uri == "ctx://obs/obs_a"
    assert result.handles[0].tokens_raw == 100
    first, second = fold_raw_events([big_a, small, big_c, tail], k=3), result
    assert first.ledger[0].block_id == second.ledger[0].block_id
    ContextBlock.model_validate(result.ledger[0].model_dump())
    print("epoch_folder.py self-check: OK (k=3 fold, tail appended, deterministic)")
