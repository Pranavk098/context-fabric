# MCP server — the fabric as an agent tool (stdio, v0)

## What this is

MCP (Model Context Protocol) is how agent tools discover capabilities. An MCP client —
Claude Desktop, Claude Code, or any other MCP-speaking app — launches a server process and
asks it, over a small JSON-RPC conversation: *what can you do?* and then *do it*. This repo
ships a minimal MCP **stdio server** (`sidecar/mcp.py`, launched as `cfx mcp` or
`python -m sidecar.mcp`) that exposes the Context Fabric to any MCP client:

- **`memory_search`** — query fabric blocks, with quarantine respected by default.
- **`receipt_verify`** — verify a signed epoch receipt chain (the SEAL).
- **`memory_add`** — append a block through the same quarantine path as `cfx add`.

## Spec revision targeted

**MCP `2025-11-25`** — the latest revision of the *handshake era*
(`initialize` → `notifications/initialized` → requests). During `initialize` the server
negotiates: it echoes `2025-11-25`, `2025-06-18`, or `2024-11-05` when the client requests
one of those (their tools wire format is identical), and otherwise counter-offers
`2025-11-25`, as the spec's version-negotiation rule requires.

The spec's *current* revision, `2026-07-28`, removed `initialize` and server-side sessions
in favour of per-request metadata. A client that only speaks the modern era will not
complete a handshake with this server; the minimal server deliberately targets the
handshake-era revision that today's stdio clients negotiate.

Framing: UTF-8, one JSON object per line, messages MUST NOT contain embedded newlines.
stdout carries protocol messages only; every log line goes to stderr.

## Client configuration

Claude Desktop (`claude_desktop_config.json`) — absolute paths because clients launch the
command from their own working directory:

```json
{
  "mcpServers": {
    "context-fabric": {
      "command": "uv",
      "args": [
        "--directory", "C:\\dev\\Context-Fabric",
        "run", "cfx", "mcp",
        "--store", "C:\\dev\\Context-Fabric\\fabric.db"
      ]
    }
  }
}
```

Generic MCP client entry (any client that spawns a subprocess, e.g. via
`mcpServers` config or `claude mcp add`):

```
command: uv
args:    run cfx mcp --store fabric.db          # run from the repo root, or use --directory
```

Equivalently without the console script: `python -m sidecar.mcp --store fabric.db`.
Add `--readonly` to refuse writes. Add `--trusted` to make `memory_add` write `routable`
blocks instead of quarantined drafts (a server-side setting — the tool arguments cannot
mark a block trusted). If `--store` is omitted, `CF_FABRIC` is read from the environment.

## Tools

| Tool | Parameters (JSON Schema) | Returns |
| --- | --- | --- |
| `memory_search` | `query: str` (required), `limit: int = 5` (capped at 50), `include_quarantined: bool = false` | JSON text: `{query, results: [{block_id, block_kind, content_excerpt (≤300 chars), lifecycle_status, score}]}` |
| `receipt_verify` | `epochs_json: str` (required; JSON array of epoch objects) | JSON text: `{ok: bool, epochs_verified?/key_id?}` or `{ok: false, reason}` |
| `memory_add` | `block_kind: str`, `content: str` (both required) | JSON text: `{block_id, content_hash, lifecycle_status, source_event_seq, deduplicated}` |

### memory_search

Scoring is **v0 local lexical**: each query is tokenized to lowercase alphanumeric words,
each block scores the number of query-token occurrences in its content (or `ref_uri` when
the block is a pointer), matches are ranked by score with store order as the tiebreak.
No embeddings, no index. This is a deliberately replaceable scorer behind the same tool
signature — when the BM25 work lands, `memory_search` should delegate to it. Only blocks
with a nonzero score are returned; `content_excerpt` truncates to 300 characters with a
trailing `...` when longer.

### receipt_verify

Reuses `protocol.epoch.verify_chain` exactly as `cfx verify` does: revalidates each
epoch's signature, Merkle root, and derived `epoch_id`, then checks `prev_epoch_hash`
linkage, tenant consistency, and ordering. The out-of-band Ed25519 public key (hex) is
read from the server process's **`CF_RECEIPT_PUBKEY`** environment variable — set it in
the client config's `env` block when you want receipt verification:

```json
{"mcpServers": {"context-fabric": {"command": "uv", "args": ["--directory", "...", "run", "cfx", "mcp", "--store", "fabric.db"], "env": {"CF_RECEIPT_PUBKEY": "..."} }}}
```

Missing or malformed key, key_id mismatch, tamper, reorder, or bad signature all return
`ok: false` with a `reason` string (a normal tool result, not a protocol error).

### memory_add and quarantine

Writes go through the same `build_block` + `SqliteStore.append` path as `cfx add` and
`POST /fabric/blocks`. Blocks are stored as **`draft`** (quarantined) unless the server was
started with `--trusted`: the kernel policy gate denies drafts (reason `lifecycle:draft`)
and `memory_search` hides them unless the caller passes `include_quarantined: true`.
`memory_add` takes **no `trusted` argument** — trust is server-side only, so a client cannot
mark its own block routable (audit AU2-H2). Identical content deduplicates to the existing
block and reports `deduplicated: true`.

## Readonly mode

`cfx mcp --store fabric.db --readonly` serves `memory_search` / `receipt_verify` only:
`memory_add` returns JSON-RPC error `-32001` (`readonly mode: memory_add is disabled`),
and a missing store file is an error instead of being created.

## Error model

| Situation | Behaviour |
| --- | --- |
| Malformed JSON line | `-32700` parse error, `id: null`; the server keeps serving |
| Non-JSON-RPC object | `-32600` invalid request |
| Unknown request method | `-32601` method not found |
| Unknown tool, bad/missing arguments, invalid `epochs_json` | `-32602` invalid params |
| Store/conflict errors inside a tool | `-32603` internal error |
| `memory_add` under `--readonly` | `-32001` implementation-defined server error |
| Any notification (including `notifications/initialized` and unknown ones) | Ignored, never answered |

## Limits (v0)

- **stdio only** — no Streamable HTTP transport, so no remote clients and no auth surface.
- **Handshake era only** — targets `2025-11-25`; the modern `2026-07-28` revision is out of
  scope. Unknown older versions get a `2025-11-25` counter-offer.
- **Lexical search only**, single page (no `nextCursor`/pagination), no resources, prompts,
  logging capability, subscriptions, sampling, or list-changed notifications.
- One tool call at a time on a single thread; one store per process (the `SqliteStore` v0
  single-writer assumption).
- `receipt_verify` needs `CF_RECEIPT_PUBKEY` in the server environment; it does not accept
  per-call keys, so a hostile client cannot choose the key it is checked against.
