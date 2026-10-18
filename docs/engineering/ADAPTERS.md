# Raw-LLM adapters (`adapters/raw_llm.py`) — v0

Wave 3 of the adapter surface: turn a compiled `ContextPacket` into provider-shaped
request fragments that place cache hints where each cloud API actually understands
them. v1.0 adapter scope is `raw_llm` + `vllm`/`lmcache` (`ADR-0004`); this module is
the cloud half. No network calls, no provider SDKs — pure deterministic builders over
`sidecar.render.render_packet`.

## Why the two providers differ

"Prompt caching" is not one feature across cloud APIs:

- **OpenAI caches automatically on identical prefixes.** The platform checks the
  leading bytes of the exact request (minimum ~1,024 tokens for current models) and
  discounts the hit; the Chat Completions API has no `cache_control`-style breakpoint
  parameter to set (checked 2026-10-18). Because the renderer already emits the shared
  Tier 0 cartridge markdown first, prefix identity is a property of the render order:
  there is nothing for the adapter to set, so `openai_request_extras` returns `{}`.
- **Anthropic caches only up to explicit breakpoints.** The Messages API caches the
  prompt prefix ending at a block you mark with
  `cache_control: {"type": "ephemeral"}`; without a breakpoint, nothing is cached. So
  the adapter splits the system prompt into two blocks and pins exactly the shared
  Tier 0 block.

## API

### `shared_prefix(packet: ContextPacket) -> str`

The exact shared bytes: `packet.tiers.tier0.content_markdown`, or `""` without a
cartridge. It is the leading substring of `render_packet(packet)` by construction
(asserted in `tests/test_adapters.py`), identical across agents on the same
store/cartridge, and the same bytes `shared_prefix_hash` hashes
(`kernel/router/hints.py`) — the token-prefix identity both providers' caches can
reuse.

### `openai_system_message(packet) -> dict[str, str]`

```python
{"role": "system", "content": render_packet(packet)}
```

### `openai_request_extras(packet) -> dict[str, object]`

Always `{}`. See "why the two providers differ": ordering already does the work, and
this API has no cache parameter to set. `prompt_cache_key` exists but is a
routing/cache-placement knob, deliberately out of v0. The function exists so call
sites keep a stable `{**request_body, **extras}` shape if a future API revision adds a
cache parameter.

### `to_anthropic(packet, cache_ttl=None) -> dict[str, list[dict[str, object]]]`

With a cartridge — two text blocks, one cache breakpoint exactly at the end of the
shared prefix:

```python
{
    "system": [
        {
            "type": "text",
            "text": "<tier0 markdown>",
            "cache_control": {"type": "ephemeral"},
        },  # or {"type": "ephemeral", "ttl": "1h"}
        {"type": "text", "text": "<instruction + tiers + tail>"},
    ]
}
```

Block 2 is `render_packet(packet)` minus the leading shared prefix and the join
newline, so `"\n".join(block["text"] for block in blocks) == render_packet(packet)`
byte for byte (tested). The remainder block is omitted when the packet is all Tier 0
(Anthropic rejects empty text blocks); the system is then just the pinned prefix block.

Without a cartridge — a single unpinned block holding the full render. Nothing is
shared across agents, so there is no breakpoint worth pinning.

`cache_ttl`: `None` (plain `{"type": "ephemeral"}`, the documented 5-minute default),
`"5m"`, or `"1h"` → the documented `cache_control.ttl` field. Any other value raises
`ValueError` before it can reach the API. Ignored when there is no cartridge.

Determinism: no clock, no RNG; same packet and same `CF_RECAP` environment produce the
same fragment.

## Layering

`adapters` imports the renderer from `sidecar.render`. The renderer is the single
source of render order, and the cache-alignment guarantee (Tier 0 first, byte-stable
across agents) is a property of that order, tested in `tests/test_render.py` and
`tests/test_adapters.py`. The sidecar is a consumer of packets like any adapter, not a
privileged layer above this one, so `adapters → sidecar` is an ordinary dependency.
If the renderer later moves to `protocol/` or `kernel/`, only the import changes.

## Honest limits

- **No Gemini adapter in v0.** v1.0 scope is `raw_llm` (OpenAI + Anthropic) plus
  vLLM/LMCache; Gemini is deferred.
- **One cache breakpoint.** v0 pins only the shared Tier 0 prefix. The framework
  adapter spec's per-agent Tier 1 breakpoint (`end of Tier 0 and Tier 1`) is deferred;
  Anthropic allows up to 4 explicit breakpoints, so there is headroom.
- **Minimum cacheable-token thresholds are not validated.** OpenAI currently caches
  prefixes of ~1,024+ tokens (model-dependent); Anthropic's minimum is model-dependent
  too. A short Tier 0 simply will not produce a cache hit — the fragments are still
  valid requests.
- **TTL variants are limited to what the provider documents.** Only `"5m"`/`"1h"` are
  accepted; other TTL/provider-versioned knobs are out of v0.
- **Fragments only.** These functions build request fragments; they do not call
  providers, and no test touches the network. The abstract `FabricAdapter` lifecycle
  from `docs/design/Framework-Adapter-Spec.md` (deliver/observe/cancel, …) is not
  built yet.

## Sources (checked 2026-10-18)

- OpenAI prompt caching (automatic prefix caching, ~1,024-token minimum,
  `prompt_cache_key` routing behavior): <https://developers.openai.com/api/docs/guides/prompt-caching>
- Anthropic prompt caching (explicit breakpoints, up to 4, `ttl` support):
  <https://platform.claude.com/docs/en/build-with-claude/prompt-caching>
- Anthropic Messages API (`cache_control.ttl: "5m" | "1h"`, default `"5m"`):
  <https://docs.anthropic.com/en/api/messages>
