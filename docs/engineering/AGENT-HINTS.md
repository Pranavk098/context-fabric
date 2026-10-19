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

## vLLM `KvHintsEnvelope` adapter (PR #53423)

`kernel/router/hints.py` still emits the flat RFC #52113-shaped dict. On top of that,
`kernel/router/kv_hints_envelope.py` maps it to the merged vLLM envelope schema:

```json
{
  "protocol_version": "0.1",
  "message_id": "sha256:<content-derived>",
  "actions": [
    {
      "action_id": "cfx_cache_hint",
      "action_type": "context_fabric.cache_hint",
      "action_version": "1.0",
      "payload": {
        "session_id": "w1:task1",
        "prefix_hash": "sha256:...",
        "shared_prefix_hash": "sha256:...",
        "ttl_seconds": 3600,
        "evict_priority": "normal"
      }
    }
  ]
}
```

- **Source schema:** `vllm/v1/kv_hints/protocol.py`, merged in vLLM PR
  [#53423](https://github.com/vllm-project/vllm/pull/53423) (merged 2026-09-21, merge
  commit `986e217`, merged by `njhill`). Verified against vLLM 0.31.0 in the WSL venv
  (`~/.venvs/vllm`), which ships `vllm/v1/kv_hints/` with `KvHintAction` +
  `KvHintsEnvelope` byte-for-byte identical to `main`.
- **One action carries all five mapped fields verbatim.** `hint_version` is dropped: the
  envelope versions itself via `protocol_version`/`action_version`. `action_type` is
  namespaced to us because vLLM defines no action semantics yet.
- **`message_id` is content-derived** (sha256 of the canonicalized action list), so
  identical hints produce an identical envelope — keeping the compile path's ADR-0003
  determinism. A live orchestrator that needs per-request uniqueness would assign it.

The sidecar attaches it via `cfx serve --kv-hints` (env `CF_KV_HINTS`), **off by default**.
Off, the forwarded request is byte-identical to before; on, the compiled request gains a
top-level `kv_hints` field. Only compile mode has a packet to hint from, so passthrough is
unaffected either way.

### What vLLM does with it today (0.31.0)

The envelope is **transport plumbing only** — it changes no cache behavior on its own:

- Plumbed through the Python engine API (`AsyncLLM.generate(..., kv_hints=...)`,
  `LLMEngine.add_request`, `EngineCoreRequest` -> scheduler `Request.kv_hints`) and the
  gRPC `GenerateRequest.kv_hints` (proto field 17).
- Forwarded to the KV offloading connector (`ReqContext.kv_hints`); the only reader today
  is the KVCR tier manager (`vllm/v1/kv_offload/tiering/kvcr/manager.py`), which just
  forwards it to the external `kvcr` package's `submit_hint`. KVCR (#53624) is still open.
- The OpenAI-compatible REST entrypoint does **not** parse a `kv_hints` request field
  (no `kv_hints` reference under `vllm/entrypoints/` in 0.31.0). So attaching `kv_hints`
  to an OpenAI `/v1/chat/completions` body currently reaches an upstream that ignores it.
- The built-in scheduler executes no TTL/evict/prefetch action from `kv_hints`, and vLLM
  defines no `action_type` values.

Net: this is forward-compatible scaffolding. `--kv-hints` sends structured intent; it
changes nothing measurable until vLLM ships a consumer (KVCR or a scheduler action) *and*
its OpenAI surface ingests the field. The original wiring trigger (RFC #52113 merging) is
unchanged — see `ROADMAP-DECISIONS.md` #1.

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

`tests/test_kv_hints_envelope.py`: envelope shape matches the vLLM schema, round-trips all
five fields losslessly, is deterministic, message-id changes with content, rejects invalid
hints, validates/decodes against the envelope schema, and integrates with
`packet_cache_hints`. `tests/test_kv_hints_sidecar.py`: the default (off) forwards no
`kv_hints` field, and the opt-in attaches a well-formed envelope to the compiled request.

## Trigger to complete the loop

Do the wiring when one of these lands:

1. **RFC #52113 merges / vLLM accepts agent hints on the OpenAI surface** -> the envelope
   adapter (`kernel/router/kv_hints_envelope.py`) already produces the merged PR #53423
   schema and `--kv-hints` attaches it; the remaining gap is vLLM's OpenAI HTTP entrypoint
   actually ingesting `kv_hints` (today it ignores it) and a consumer (KVCR #53624 or a
   scheduler action) acting on it.
2. **LMCache connector sprint** -> translate `session_id` / `ttl_seconds` /
   `evict_priority` to LMCache pin/TTL/evict APIs and measure hit-rate / TTFT lift in the
   eval harness.

Acceptance for "wired": an eval run where enabling hints measurably improves cache hit
rate or TTFT against an identical run without them. Until then, the trace field plus the
opt-in envelope are the whole story.
