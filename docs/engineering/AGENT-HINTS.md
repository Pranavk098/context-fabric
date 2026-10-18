# Agent Hints - the computable slice of vLLM RFC #52113

## What this is

`kernel/router/hints.py` turns a compiled `ContextPacket` into a deterministic hint dict:

```python
from kernel.router.hints import packet_cache_hints

hints = packet_cache_hints(packet)
# {"session_id": "w1:task1", "prefix_hash": "sha256:...", "shared_prefix_hash": "sha256:...",
#  "ttl_seconds": 3600, "evict_priority": "normal", "hint_version": "0.1"}
```

`compile_packet` records the same dict in its trace under `cache_hints`, so every
compiled packet carries the hints alongside `included`/`scores`/`excluded`.

## Why

Apps know future reuse better than the engine. The engine sees a stream of tokens; the
app knows that step 7 of a workflow will resend the same cartridge and decisions, that a
task will end in ten minutes, and that some contexts are worth pinning. vLLM's
session-aware KV hints RFC
([issue #52113](https://github.com/vllm-project/vllm/issues/52113)) proposes exactly this
app-to-engine `agent_hint` metadata (session ID, retention TTL, explicit evict/prefetch
ops). CacheScout's agent-transition work and Continuum's TTL work point the same way
(see `docs/research/SOTA-AND-THREATS.md` §3.2).

**Status: RFC #52113 is OPEN and not shipped.** No vLLM (or LMCache) API accepts these
hints today. Rather than wait, we ship the computable slice now: a deterministic,
tested hint producer whose output sits in the compile trace, with a documented mapping to
the RFC concepts.

**Honest note: we do not send these hints anywhere yet.** No engine reads them, nothing
changes cache behavior, no latency claim follows from this. They are structured intent,
recorded locally, ready for the wiring step.

## Field -> RFC mapping

The RFC is not finalized, so this maps our fields to the concepts as described in the
issue, not to a frozen wire schema:

| Our field | RFC #52113 concept | Value here |
| :--- | :--- | :--- |
| `session_id` | session identity, groups requests that may reuse context | `"<workflow_id>:<task_id>"` from the packet scope |
| `prefix_hash` | identity of the reusable rendered prompt head (per-agent) | `sha256:` over the RFC 8785 canonical form of `{"tier0": <cartridge markdown>, "instruction": <instruction content>}` |
| `shared_prefix_hash` | identity of the token prefix agents on the same cartridge can actually share | `sha256:` over the Tier 0 cartridge markdown bytes alone (same encoding the serializer uses, so it equals `Tier0Cartridge.kv_prefix_hash` for a compiled cartridge); `""` when there is no Tier 0 (small projects) |
| `ttl_seconds` | retention TTL | default `3600`, keyword-parameterizable |
| `evict_priority` | explicit eviction hint | `"low"` / `"normal"` / `"high"`, from `total_tokens / hard_total_tokens` (>= 75% high, >= 25% normal; large cached contexts are the most expensive to rebuild) |
| `hint_version` | schema version for forward compatibility | `"0.1"` |

### What the prefix hashes cover, and why

The renderer emits the shared Tier 0 cartridge markdown first (it is identical for every
agent on the same store/cartridge), then the per-agent instruction. Two hashes reflect
that two-level identity:

- `prefix_hash`: Tier 0 cartridge markdown plus the instruction block's content,
  canonicalized (RFC 8785) and hashed as a pair. This is the identity of the rendered
  prompt head; it is per-agent because the instruction is.
- `shared_prefix_hash`: the Tier 0 cartridge markdown bytes alone. This is the token
  prefix two agents could actually share in a KV cache, and it is stable across
  objectives and agents on the same cartridge. Empty string when there is no Tier 0, so
  "no shared prefix" is explicit rather than a hash of nothing.

Everything else is excluded from both, on purpose:

- Tier 3 observations and Tier 1 blocks are per-agent, per-step suffixes. Including
  them would make the hashes change on every turn, destroying the prefix identity that a
  KV cache needs to hit.
- Tier 0 renders first and the instruction immediately after, so these two byte strings
  are genuinely the leading bytes of every rendered prompt for the session.

Note these are *our* packet-level prefix identities, not engine KV block hashes. When
wired into an engine they must be reconciled with the engine's own prefix hashing (vLLM
hashes token blocks; it cannot consume our JSON hash directly).

## Tests

`tests/test_hints.py`: determinism (same packet -> identical hints), prefix hash changes
when instruction or Tier 0 changes and stays stable when Tier 3 observations change,
`shared_prefix_hash` equals the sha256 of the Tier 0 markdown (and the cartridge's own
`kv_prefix_hash`), is empty without Tier 0, and is equal for two agents compiled with
different objectives while `prefix_hash` differs, field types and version, TTL
default/override, and the eviction priority rule. `tests/test_render.py` proves the byte
guarantee: two agents on one store/cartridge render identical leading bytes up to the end
of the Tier 0 markdown (no objective text inside), divergence starting exactly after it,
and the empty-Tier 0 render byte-identical to the old instruction-first order.

## Trigger to complete the loop

Do the wiring when one of these lands:

1. **RFC #52113 merges / vLLM accepts agent hints** -> emit the hint dict into the
   request path (per-request metadata or engine API), not just the trace.
2. **LMCache connector sprint** -> translate `session_id` / `ttl_seconds` /
   `evict_priority` to LMCache pin/TTL/evict APIs and measure hit-rate / TTFT lift in the
   eval harness.

Acceptance for "wired": an eval run where enabling hints measurably improves cache hit
rate or TTFT against an identical run without them. Until then, the trace field is the
whole story.
