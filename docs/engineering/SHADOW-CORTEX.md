# Shadow Cortex v0 — deterministic pre-execution probes

Written 2026-10-18. Code: `kernel/router/shadow_cortex.py`. Tests:
`tests/test_shadow_cortex.py`.

## What this is

The Shadow Cortex runs **deterministic probes before the LLM** so the agent does not spend
tool calls and tokens rediscovering context it was already handed (Arch-Spec invariant 4,
"Zero-Token Cold Boot"). Since Track C/D the compiler and packer already carry
`kind == shadow_probe` blocks into Tier 3 and the renderer prints them, but nothing
generated or evaluated probes. This module closes that gap for the context we actually
have:

- `generate_probes(blocks, *, limit=3, trust_domain="default", event_seq=0, as_of)` mints
  `shadow_probe` blocks from the most critical existing blocks —
  `negative_knowledge` > `decision` > `fact`.
- `evaluate_probes(response_text, probes)` returns one `ProbeVerdict` per probe:
  `acknowledged` / `contradicted` / `ignored`.

## Spec and divergences

Spec: `docs/design/Module-Implementation.md` §Module B (lines 33–43) — "Execute
deterministic pre-execution probes in under 15ms before compiling the packet so the agent
does not waste tool calls searching for obvious context", producing
`ContextBlock(kind="shadow_probe", memory_tier=3, creator_type="deterministic_probe",
fidelity="extract")`. Supporting spec: `Arch-Spec.md` line 21 (invariant 4), line 55
(Tier 3 contains "pre-executed `shadow_probe` slices"), line 74 (`Prune(i)` highest for
`negative_knowledge`/`shadow_probe`), line 85 (>25ms timeout → emit packet without probes,
`probe_skipped`); block kind and creator type are schema in
`ADR-0005-packet-schema-v1.md` lines 25/34.

**Divergences (v0):**

1. **The spec's three probes are not implemented.** Probe 1 (stack-trace file:line →
   tree-sitter AST slice), Probe 2 (symbol impact via `symbols_exported`/`imported`
   query), Probe 3 (`git log -n 3` on target files) all introspect external state. v0
   generates probes from blocks already in the Fabric instead. **This is the remaining
   gap.** No git, no AST, no store query, no filesystem access yet.
2. **tree-sitter → stdlib `ast`** is the repo-wide cartridge delta (status note,
   `Module-Implementation.md` line 5); Probe 1 would follow it when built.
3. **No timeout / degraded `probe_skipped` path.** Generation is in-memory string
   templating, so there is nothing to time out yet; the >25ms fail-open rule becomes
   relevant when probes touch git/AST/store.
4. **Not wired into a compile path automatically.** `sidecar/compile` and
   `compile_packet` do not call the Shadow Cortex; a caller generates probes and passes
   them as ordinary blocks. The compiler already routes them correctly
   (`compiler.py:114,146,176`, `packer.py:180–187`, `sidecar/render.py:79`), and they count
   against the Tier 3 budget.
5. **Evaluation has no consumer.** No receipt/route-trace field records verdicts; the
   caller receives them from `evaluate_probes` and decides what to do (log, store,
   escalate). No enforcement happens.

## Probe format

One probe per selected source block. Deterministic templates (first 12 whitespace words
of the source as the fragment), plus a traceability tag:

| source kind | probe content |
| :--- | :--- |
| `decision` | `Still true: <fragment>? Answer yes/no. [probe-of:<block_id>]` |
| `negative_knowledge` | `Do not repeat: <fragment>? Confirm avoid. [probe-of:<block_id>]` |
| `fact` | `Confirm: <fragment>? Answer yes/no. [probe-of:<block_id>]` |

Examples:

```
Still true: Use Postgres for canonical storage? Answer yes/no. [probe-of:d1]
Do not repeat: Do not retry naive masking of tokens? Confirm avoid. [probe-of:nk1]
Confirm: The router is deterministic? Answer yes/no. [probe-of:f1]
```

The probe block is `blk_probe_<source_block_id>`, `memory_tier=register`,
`lifecycle_status=routable`, `trust_domain=<as passed>`, `source_event_seq=<as passed>`
(use the packet watermark), `observed_at=valid_from=as_of`, sha256 content hash, token
count from the pinned `o200k_base` tokenizer. Selection is by
`(kind priority, source_event_seq, block_id)` — a total order — and `limit` caps the
count.

## Evaluation

`evaluate_probes` is keyword-level (documented v0 ceiling). For each probe:

1. Anchor the response: the `[probe-of:<id>]` tag if present, else the longest word-run of
   the fragment found (tries full fragment, then 6, 4, 3 words; e.g. "do not retry naive
   masking" is anchored by an answer containing "naive masking of tokens").
2. Find cues case-insensitively on the anchor's line; if none, the following line; then
   the preceding line. The cue **nearest** to the anchor wins (ties → earlier entry).
3. Verdict: an acknowledgment cue → `acknowledged`; a contradiction cue →
   `contradicted`; no anchor or no cue → `ignored`.

Cue lists (constants in the module):

```python
ACK_CUES = (
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
CONTRA_CUES = (
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
```

Echoed template phrases (`Answer yes/no`, `Confirm avoid`, `Still true:`, `Confirm:`,
`Do not repeat:`) are stripped before matching so quoting the question cannot read as
answering it. Line scoping keeps neighboring probes' answers from bleeding in.
`ProbeVerdict` carries `(block_id, verdict, cue)` in probe order.

## Usage

```python
from kernel.router.shadow_cortex import evaluate_probes, generate_probes

probes = generate_probes(
    blocks, limit=3, trust_domain=scope.tenant_id, event_seq=watermark, as_of=as_of
)

# Pass through the compiler: probes land in tier3.shadow_probes (read-only path already).
result = compile_packet(..., blocks=[*blocks, *probes], as_of=as_of)
text = render_packet(result.packet)  # "[shadow_probe] Still true: ..." lines

# After the agent responds (persist probes first if they must survive the turn):
verdicts = evaluate_probes(agent_response_text, result.packet.tiers.tier3.shadow_probes)
```

Alternatively persist the generated blocks via the store (they are already valid routable
`ContextBlock`s) so a later compile recalls them; the sidecar HTTP endpoint currently
quarantines writes as `draft`, which the policy engine then denies — persist through a
path that keeps them `routable`, or keep them in-memory for the turn.

## Determinism

ADR-0003 applies: no clock, no RNG, no I/O. `as_of` is a required parameter; `event_seq`
is stamped in. Selection and evaluation orderings are total orders (ties on block id /
earlier cue). Same inputs → byte-identical probes; `test_generation_is_deterministic_and_
input_order_independent` asserts `model_dump()` equality, and the compile integration test
asserts an identical packet `content_hash`.

## Tests

`tests/test_shadow_cortex.py` (12): priority + limit + non-candidate kinds, determinism
and input-order independence, round-trip validation and probe shape, policy gates
(trust-domain pass/deny), fragment traceability, acknowledged (tagged and untagged),
contradicted with echoed question, ignored/empty/unattributable bare cue, case
insensitivity, unicode fragment, and compile integration (probes in Tier 3, no
instruction/observation duplication, within budget, stable hash).

## v0 limits (honest)

- **No external introspection yet** — the headline gap vs the spec (Probes 1–3 above).
- **Keyword NLU ceiling:** inverted/passive phrasing, anaphora ("do not use it"), and
  multi-clause negation are not understood; `actually` / `no longer` anywhere near the
  anchor outweigh a later faint cue; a bare `Yes.` with neither tag nor fragment is
  `ignored` (unattributable across probes).
- **Echo stripping is a trade:** a literal bare answer "Still true:" is stripped as an
  echo and reads `ignored`.
- **No verdict persistence or enforcement** — callers decide what to do with verdicts.
- **<15ms objective is structural, not benchmarked:** generation/evaluation are in-memory
  string ops; no timing harness was added.
- **Three source kinds only**, 12-word fragments, default `limit=3`; `ref_uri` is used as
  fragment fallback when a block has no content.

## Trigger to complete the loop

Wire it when a caller needs it: (a) the sidecar compile path auto-generates probes from
recalled blocks, (b) Probes 1–3 land with the >25ms timeout → `probe_skipped` degraded
mode, and (c) receipts record `ProbeVerdict`s so acknowledgment can be measured across a
workflow instead of logged per call.
