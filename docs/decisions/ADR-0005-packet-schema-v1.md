# ADR-0005 — CPP v1.0 Frozen Packet Schema

- **Status:** Accepted (frozen for Stage B; changes require a new ADR)
- **Date:** 2026-10-16
- **Related:** `Db-Storage-Spec.md` (`context_blocks`, `compiled_packets`), `Module-Implementation.md`.

## Context

The adapters, compiler, and evals all depend on one exact packet shape. The original docs
described tiers and fields in prose but never froze a wire schema. Stage B needs a concrete
schema to implement `protocol/models.py` against.

## Decision

Freeze the following **logical** schema. Field names and semantics are normative; the JSON
below is the canonical (`cpp_version = "1.0"`) representation. The adapter may re-render the
tiers into provider-native messages, but the logical packet — and therefore `content_hash` —
must not change.

### ContextBlock (the unit stored in `context_blocks` and embedded in tiers)

```jsonc
{
  "block_id": "blk_01J9...",
  "block_kind": "instruction | decision | fact | artifact | observation | summary | pointer | negative_knowledge | shadow_probe | cartridge_hologram",
  "memory_tier": 0,                  // 0..3
  "media_type": "text/markdown",
  "content": "…",                    // null if pure pointer
  "ref_uri": null,                   // "ctx://object/…" when content is a pointer
  "content_hash": "sha256:…",
  "token_estimate": 0,
  "tokenizer_name": "o200k_base",
  "source_event_seq": 0,
  "creator": { "type": "human|agent|tool|deterministic_probe", "id": "…" },
  "confidence": 1.0,
  "fidelity": "raw | extract | summary | pointer",
  "verified": false,
  "observed_at": "2026-10-16T00:00:00Z",
  "valid_from": "2026-10-16T00:00:00Z",
  "valid_to": null,
  "lifecycle_status": "draft|validated|routable|superseded|expired|redacted",
  "classification": "internal",
  "trust_domain": "…",
  "allowed_roles": [],
  "denied_roles": [],
  "allowed_purposes": [],
  "symbols_exported": [],
  "symbols_imported": [],
  "counterfactual_meta": null        // set for negative_knowledge
}
```

### ContextPacket (the compiled artifact stored in `compiled_packets`)

```jsonc
{
  "cpp_version": "1.0",
  "packet_id": "cpk_01J9...",
  "parent_packet_id": null,          // set for COW_FORK children
  "route_id": "rte_01J9...",
  "spawn_depth": 0,
  "epoch_number": 1,
  "watermark_event_seq": 12345,      // see ADR-0002
  "scope": { "tenant_id": "…", "project_id": "…", "workflow_id": "…", "task_id": "…" },
  "recipient": { "agent_id": "…", "role": "…", "stage": "…" },
  "budget": { "hard_total_tokens": 4000, "tier0": 600, "tier1": 1000, "tier2": 200, "tier3": 1800, "reserve": 400 },
  "tiers": {
    "tier0": { "cartridge_id": "cfx_…", "kv_prefix_hash": "…", "content_markdown": "…" },
    "tier1": { "blocks": [ /* ContextBlock[] in ascending event_seq */ ] },
    "tier2": { "handles": [ { "uri": "ctx://obs/…", "summary": "…", "tokens_raw": 0, "sha256": "…" } ] },
    "tier3": {
      "instruction": { /* ContextBlock */ },
      "shadow_probes": [ /* ContextBlock */ ],
      "observations": [ /* ContextBlock, append-only within an epoch */ ],
      "recency_anchor": { "output_contract": "…", "prohibitions": [ "…" ] }
    }
  },
  "conflict_alerts": [ { "a_block_id": "…", "b_block_id": "…", "reason": "contradicts" } ],
  "total_tokens": 3400,
  "hashes": { "content_hash": "sha256:…", "signature": "ed25519:…", "key_id": "…" },
  "route_trace_ref": { "included": [], "excluded": [] }
}
```

### Rules

1. `content_hash` = sha256 over RFC 8785 JCS of the packet **excluding** `hashes.signature`
   (ADR-0003 §5).
2. `total_tokens` counts Tiers 0–3; the packing assertion is
   `total_tokens + reserve_used <= hard_total_tokens`.
3. `tier1.blocks` order is authoritative and ascending by `event_seq` (ADR-0002).
4. Unknown fields are rejected (`pydantic` `model_config = ConfigDict(extra="forbid")`) so
   the schema cannot drift silently.

## Consequences

- `protocol/models.py` implements exactly this. Any change is a new ADR + version bump.
- `WorkflowEvent` and `AgentOutcome` schemas (referenced by the API) will be frozen in the
  same manner before Stage C; they are out of scope for the Stage B spike.
