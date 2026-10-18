"""Shadow Cortex — deterministic pre-execution probes (Module B, v0 context-derived subset).

Spec: ``docs/design/Module-Implementation.md`` §Module B — execute deterministic
pre-execution probes in under 15ms before compiling the packet so the agent does not waste
tool calls (Arch-Spec invariant 4, "Zero-Token Cold Boot"). v0 generates probes from
existing context blocks — negative knowledge > decisions > facts — and evaluates the
agent's response against each probe's expectation. It does NOT yet introspect stack
traces, ASTs, symbols, or git; that is the announced remaining gap
(``docs/engineering/SHADOW-CORTEX.md``).

Determinism (ADR-0003): no clock, no RNG, no I/O. ``as_of`` is a required parameter and
every ordering is a total order, so identical inputs produce byte-identical probes.

Evaluation is keyword-level by design (v0 ceiling): the answer must carry the probe's
``[probe-of:<block_id>]`` tag or a run of the probe's fragment words; the nearest
acknowledgment/contradiction cue is then searched on the anchor's line first, then the
following line, then the preceding line, so neighboring probes' answers on other lines do
not bleed in. Echoed template phrases ("Answer yes/no", "Confirm avoid", "Still true:",
"Confirm:", "Do not repeat:") are stripped before matching so a quoted question cannot
read as an answer. See the v0 limits in ``docs/engineering/SHADOW-CORTEX.md``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from kernel.cartridge.serializer import count_tokens
from protocol.canonical import sha256_hex
from protocol.models import (
    BlockKind,
    ContextBlock,
    Creator,
    CreatorType,
    Fidelity,
    LifecycleStatus,
    MemoryTier,
)

PROBE_TAG_PREFIX = "probe-of:"
FRAGMENT_WORDS = 12
CREATOR_ID = "shadow_cortex"

ACKNOWLEDGED = "acknowledged"
CONTRADICTED = "contradicted"
IGNORED = "ignored"

# Case-insensitive cue lists. Contradiction cues are listed first: an exact distance tie
# resolves to the earlier entry (deterministic, and "no" over a same-offset "no longer").
ACK_CUES: tuple[str, ...] = (
    "yes",
    "confirm",
    "confirmed",
    "still true",
    "reaffirm",
    "reaffirmed",
    "acknowledged",
    "noted",
    "correct",
    "avoid",
    "avoided",
)
CONTRA_CUES: tuple[str, ...] = (
    "no",
    "not true",
    "no longer",
    "false",
    "incorrect",
    "actually",
    "outdated",
    "superseded",
    "reverted",
)

_KIND_PRIORITY: dict[BlockKind, int] = {
    BlockKind.negative_knowledge: 0,
    BlockKind.decision: 1,
    BlockKind.fact: 2,
}
_TEMPLATES: dict[BlockKind, str] = {
    BlockKind.negative_knowledge: "Do not repeat: {fragment}? Confirm avoid.",
    BlockKind.decision: "Still true: {fragment}? Answer yes/no.",
    BlockKind.fact: "Confirm: {fragment}? Answer yes/no.",
}

_TAG_RE = re.compile(r"\[(probe-of:[A-Za-z0-9_.:\-]+)\]")
_FRAGMENT_RE = re.compile(
    r"^(?:Still true|Confirm|Do not repeat): (.*?)\? "
    r"(?:Answer yes/no\.|Confirm avoid\.) \[probe-of:"
)
_ECHO_RE = re.compile(r"answer yes/no\.?|confirm avoid\.?|still true:|confirm:|do not repeat:")
_CUES: tuple[tuple[str, str], ...] = tuple((c, CONTRADICTED) for c in CONTRA_CUES) + tuple(
    (c, ACKNOWLEDGED) for c in ACK_CUES
)
_CUE_RE: tuple[tuple[re.Pattern[str], str, str], ...] = tuple(
    (re.compile(rf"\b{re.escape(cue)}\b"), cue, verdict) for cue, verdict in _CUES
)


@dataclass(frozen=True)
class ProbeVerdict:
    """Per-probe evaluation outcome; ``cue`` is the matched cue ("" when ignored)."""

    block_id: str
    verdict: str
    cue: str = ""


def _fragment(text: str, max_words: int = FRAGMENT_WORDS) -> str:
    return " ".join(text.split()[:max_words])


def generate_probes(
    blocks: Sequence[ContextBlock],
    *,
    limit: int = 3,
    trust_domain: str = "default",
    event_seq: int = 0,
    as_of: datetime,
) -> tuple[ContextBlock, ...]:
    """Deterministically mint ``shadow_probe`` blocks from the most critical blocks.

    Candidates are ``negative_knowledge`` > ``decision`` > ``fact``, ordered by
    ``(kind priority, source_event_seq, block_id)`` — a total order, independent of input
    order. Each probe's content embeds the first ``FRAGMENT_WORDS`` whitespace words of the
    source and a ``[probe-of:<block_id>]`` tag for traceability. ``limit`` caps the count
    and ``event_seq`` stamps every probe (use the packet watermark).
    """
    if limit <= 0:
        return ()
    candidates = sorted(
        (b for b in blocks if b.block_kind in _KIND_PRIORITY),
        key=lambda b: (_KIND_PRIORITY[b.block_kind], b.source_event_seq, b.block_id),
    )[:limit]
    probes: list[ContextBlock] = []
    for source in candidates:
        fragment = _fragment(source.content or source.ref_uri or source.block_id)
        content = (
            _TEMPLATES[source.block_kind].format(fragment=fragment)
            + f" [{PROBE_TAG_PREFIX}{source.block_id}]"
        )
        probes.append(
            ContextBlock(
                block_id=f"blk_probe_{source.block_id}",
                block_kind=BlockKind.shadow_probe,
                memory_tier=MemoryTier.register,
                content=content,
                content_hash="sha256:" + sha256_hex(content.encode("utf-8")),
                token_estimate=count_tokens(content),
                source_event_seq=event_seq,
                creator=Creator(type=CreatorType.deterministic_probe, id=CREATOR_ID),
                fidelity=Fidelity.extract,
                lifecycle_status=LifecycleStatus.routable,
                observed_at=as_of,
                valid_from=as_of,
                trust_domain=trust_domain,
            )
        )
    return tuple(probes)


def _tag_of(content: str) -> str:
    match = _TAG_RE.search(content)
    return match.group(1) if match else ""


def _fragment_of(content: str) -> str:
    match = _FRAGMENT_RE.search(content)
    if match:
        return match.group(1).strip()
    return _TAG_RE.sub("", content).strip()


def _anchor(text: str, tag: str, fragment: str) -> int:
    """Position of the tag, else of the longest fragment word-run found; -1 if absent.

    Word-runs are tried long-to-short (full fragment, then 6, 4, 3 words) and, within a
    length, first-to-last, so the anchor is deterministic and tolerates an answer that
    paraphrases the head of the fragment ("do not retry naive masking" answered as
    "naive masking of tokens").
    """
    if tag:
        pos = text.find(tag.lower())
        if pos >= 0:
            return pos
    words = fragment.lower().split()
    if not words:
        return -1
    lengths = sorted({n for n in (len(words), 6, 4, 3) if 1 <= n <= len(words)}, reverse=True)
    for length in lengths:
        for start in range(0, len(words) - length + 1):
            pos = text.find(" ".join(words[start : start + length]))
            if pos >= 0:
                return pos
    return -1


def _line_bounds(text: str, index: int) -> tuple[int, int]:
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    return start, len(text) if end < 0 else end


def _nearest_cue(text: str, anchor: int, region: tuple[int, int]) -> tuple[str, str] | None:
    start, end = region
    best: tuple[tuple[int, int], str, str] | None = None
    for pattern, cue, verdict in _CUE_RE:
        for match in pattern.finditer(text, start, end):
            key = (abs(match.start() - anchor), match.start())
            if best is None or key < best[0]:
                best = (key, verdict, cue)
    return None if best is None else (best[1], best[2])


def _verdict_at(text: str, anchor: int) -> tuple[str, str] | None:
    """Cue lookup on the anchor's line, then the following line, then the preceding one."""
    start, end = _line_bounds(text, anchor)
    regions = [(start, end)]
    next_start = end + 1
    if next_start < len(text):
        regions.append(_line_bounds(text, next_start))
    if start > 0:
        regions.append(_line_bounds(text, start - 1))
    for region in regions:
        hit = _nearest_cue(text, anchor, region)
        if hit is not None:
            return hit
    return None


def evaluate_probes(response_text: str, probes: Sequence[ContextBlock]) -> tuple[ProbeVerdict, ...]:
    """Keyword verdict per probe: acknowledged / contradicted / ignored (order preserved).

    The response must anchor to the probe (tag or fragment word-run); the nearest cue on
    the anchor line (else next line, else previous line) decides. No anchor, or no cue, is
    ``ignored``. Case-insensitive and symmetric across probes.
    """
    text = _ECHO_RE.sub(" ", response_text.lower())
    verdicts: list[ProbeVerdict] = []
    for probe in probes:
        content = probe.content or ""
        anchor = _anchor(text, _tag_of(content), _fragment_of(content))
        hit = None if anchor < 0 else _verdict_at(text, anchor)
        if hit is None:
            verdicts.append(ProbeVerdict(probe.block_id, IGNORED))
        else:
            verdicts.append(ProbeVerdict(probe.block_id, hit[0], hit[1]))
    return tuple(verdicts)


if __name__ == "__main__":

    def mk(bid: str, kind: BlockKind, content: str, seq: int) -> ContextBlock:
        return ContextBlock(
            block_id=bid,
            block_kind=kind,
            memory_tier=MemoryTier.ledger,
            content=content,
            content_hash="sha256:" + bid,
            token_estimate=1,
            source_event_seq=seq,
            creator=Creator(type=CreatorType.agent, id="a"),
            fidelity=Fidelity.extract,
            trust_domain="t",
        )

    blocks = [
        mk("f1", BlockKind.fact, "The router is deterministic", 2),
        mk("d1", BlockKind.decision, "Use Postgres for canonical storage", 1),
        mk("nk1", BlockKind.negative_knowledge, "Do not retry naive masking of tokens", 3),
    ]
    as_of = datetime(2026, 1, 1)
    probes = generate_probes(blocks, trust_domain="t", event_seq=9, as_of=as_of)
    assert [p.block_id for p in probes] == ["blk_probe_nk1", "blk_probe_d1", "blk_probe_f1"]
    assert probes == generate_probes(blocks, trust_domain="t", event_seq=9, as_of=as_of)
    assert "[probe-of:nk1]" in (probes[0].content or "")
    assert generate_probes(blocks, limit=1, trust_domain="t", event_seq=9, as_of=as_of) == (
        probes[0],
    )
    assert generate_probes(blocks, limit=0, trust_domain="t", event_seq=9, as_of=as_of) == ()

    response = (
        "Yes, still true: use Postgres for canonical storage. [probe-of:d1]\n"
        "Noted, we will avoid naive masking of tokens. [probe-of:nk1]\n"
        "Actually the router is no longer deterministic. [probe-of:f1]"
    )
    verdicts = evaluate_probes(response, probes)
    assert [v.verdict for v in verdicts] == [ACKNOWLEDGED, ACKNOWLEDGED, CONTRADICTED]
    assert [v.verdict for v in evaluate_probes("", probes)] == [IGNORED, IGNORED, IGNORED]
    print("shadow_cortex.py self-check: OK")
