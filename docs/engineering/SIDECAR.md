# Context Fabric Sidecar — the plug

The sidecar is a small local proxy that speaks the OpenAI HTTP API. Point any tool that
lets you set a custom OpenAI base URL — Continue, aider, open-webui, LangChain, or plain
SDK code — at `http://127.0.0.1:8080/v1` instead of your model server. The sidecar forwards
every chat completion to the upstream model unchanged, but first (in `compile` mode) it
treats the last user message as the objective, compiles a Context Fabric packet from your
fabric blocks, and prepends its text rendering as a system message. Every request then
carries the same bounded, prefix-stable project truth instead of re-reading a growing
chat log; the response gains `X-Fabric-Compiled: 1` and `X-Fabric-Packet-Id` headers so
you can see it happened.

`passthrough` mode forwards requests untouched and exists for honest A/B comparisons: same
tool, same task, same model, with and without compilation. `/fabric/stats` exposes
counters (requests, compiled, errors, and summed upstream `prompt_tokens` — parsed from
non-streaming JSON responses and best-effort from `usage` chunks inside SSE streams) for a
quick before/after token read; set `CF_PRICE_INPUT_PER_MTOK` (dollars per million input
tokens) to also get `estimated_input_cost_usd`. `/fabric/health` is a liveness probe.

With a SQLite fabric (`--fabric fabric.db`) the sidecar is also **writable**:
`POST /fabric/blocks` appends agent-authored blocks, quarantined as `draft` unless the
writer marks them trusted. See "Writable memory" below.

## 10-minute setup

```powershell
# 1. environment (from the repo root)
uv sync

# 2. start an OpenAI-compatible upstream — see docs/engineering/RUNTIME.md.
#    Example (WSL2 native): vllm serve Qwen/Qwen2.5-1.5B-Instruct --enable-prefix-caching --port 8000

# 3. (optional) fabric blocks: one ContextBlock JSON per line.
#    Minimal line: {"block_id":"d1","block_kind":"decision","memory_tier":1,
#    "content":"Use Postgres for canonical storage","content_hash":"sha256:d1",
#    "token_estimate":8,"source_event_seq":1,"creator":{"type":"human","id":"you"},
#    "fidelity":"extract","trust_domain":"default"}

# 4. start the sidecar (defaults: upstream http://127.0.0.1:8000, port 8080, mode compile)
uv run cfx serve --fabric fabric.jsonl

# 5. point your tool at http://127.0.0.1:8080/v1 (any API key; it is forwarded upstream)
#    check: Invoke-RestMethod http://127.0.0.1:8080/fabric/health
```

**WSL2:** vLLM runs inside WSL2, but WSL2 forwards `localhost` to Windows, so the
Windows-side sidecar reaches it at `http://127.0.0.1:8000` with no extra configuration. If
that fails, use the WSL IP (`wsl hostname -I`) as `--upstream`.

CLI flags (env fallbacks): `--upstream`/`CF_UPSTREAM`, `--port`/`CF_PORT`,
`--fabric`/`CF_FABRIC`, `--mode`/`CF_MODE`. `X-Fabric-Agent` and `X-Fabric-Role` request
headers optionally set the packet recipient (defaults: `default-agent` / `user`).
`python -m sidecar --port 9000` still works and is equivalent to `cfx serve --port 9000`
(env-only configurations work either way).

## The `cfx` command

```bash
uv run cfx serve --fabric fabric.db                    # same flags/env as python -m sidecar
uv run cfx add --store fabric.db --file block.json     # writes a quarantined draft
uv run cfx add --store fabric.db --file block.json --trusted   # writes routable
uv run cfx add --store fabric.db --file -              # read block JSON from stdin
uv run cfx verify receipts.json --pubkey <hex>         # epoch-chain check (delegates to protocol/epoch.py)
```

Compiles against a `.db` fabric are recorded as replayable compile manifests in the store;
`cfx replay` / `cfx seal` turn them into recompile checks and signed epoch receipts — see
docs/engineering/SEAL.md, "Replay & epoch sealing over real traffic".

`cfx add` prints `block_id`, `content_hash`, and `lifecycle_status`; re-adding an
identical block prints `already present` (exit 0), and reusing a `block_id` with different
content exits 1 with a conflict message. Invalid payloads exit 2, as does a store that
cannot be opened (a non-SQLite file, for example); store write errors exit 1.
`cfx serve --fabric <path>` exits 2 if the fabric file does not exist (it is never
created implicitly — run `cfx add` or `python -m kernel.store ingest` first).

## Writable memory (`POST /fabric/blocks`)

Start the sidecar with a SQLite fabric (`--fabric fabric.db` / `.sqlite`; JSONL fabrics
stay read-only) and agents can append blocks through the sidecar:

```bash
curl -s http://127.0.0.1:8080/fabric/blocks \
  -H "Content-Type: application/json" \
  -d '{"block_kind":"decision","content":"Use Postgres for canonical storage"}'
# 201 {"block_id":"blk_...","content_hash":"sha256:...","lifecycle_status":"draft","source_event_seq":1}
```

Written blocks are **quarantined**: they land as `lifecycle_status=draft`, and the kernel
policy gate denies draft blocks (denial reason `lifecycle:draft`), so they never enter a
compiled packet until promoted. Send `"trusted": true` in the body (or use
`cfx add --trusted`) to store the block as `routable` instead. v0 promotes at write time
(`--trusted`); in-place promotion is future work. Re-posting an identical block returns
`200` with `"deduplicated": true` **and the stored block's fields** — a `trusted` re-post
of an existing draft still reports (and keeps) `draft`. Reusing a `block_id` with
different content returns `409`; without a writable store (JSONL, or no `--fabric`) the
endpoint returns `501`.

The payload must carry exactly one **non-empty** `content`/`ref_uri`, a valid
`block_kind`, and an optional client `block_id` (1–128 characters, no whitespace or
control characters). Derived fields are always computed server-side and any
client-supplied `content_hash`, `token_estimate`, `source_event_seq`, `lifecycle_status`,
`memory_tier`, or `fidelity` is ignored, so a payload cannot spoof a hashed/routable
block; `"trusted": true` is the only promotion switch.

**Request framing:** every request must send `Content-Length`. Missing `Content-Length`
and `Transfer-Encoding: chunked` are rejected with `411` (the sidecar does not decode
chunked bodies), malformed `Content-Length` with `400`, and bodies over
`CF_MAX_BODY_BYTES` (default 33554432) with `413`; the `4xx` framing responses send
`Connection: close` and close the socket rather than trying to parse the unread body as a
new request.

**Security note:** the sidecar binds `127.0.0.1` only and trusts its callers — CORS
preflights are answered (`OPTIONS` → `204`) and every response, including error responses
generated by the stdlib, carries `Access-Control-Allow-Origin: *` (and exposes
`X-Fabric-Compiled`/`X-Fabric-Packet-Id`), so any local process (or prompt-injected
tooling) can read and write the fabric. Do not expose the port beyond a cooperative
single-operator machine.

**Threading model:** `ThreadingHTTPServer` runs one handler thread per connection over a
single shared `sqlite3` connection (`SqliteStore(..., check_same_thread=False)`). Writes —
including `source_event_seq` allocation and dedupe — are serialized by a lock around
build+append, since a lost event_seq race would corrupt order; reads may interleave. The
default sqlite3 build (`threadsafety == 3`) serializes access to the connection itself.
Client-supplied `Content-Length` is read with no socket timeout, and `tiktoken` tokenizes
up to `CF_MAX_BODY_BYTES` per block, so a stalled or abusive local client can tie up a
handler thread.

## Honest A/B recipe

Run the **same multi-turn task** twice against the same model and settings, restarting the
sidecar between arms:

```powershell
# arm 0 — control
uv run cfx serve --mode passthrough --fabric fabric.jsonl
# arm 1 — treatment
uv run cfx serve --mode compile --fabric fabric.jsonl
```

Compare `usage.prompt_tokens` per turn (from your tool or the upstream logs) or the
`upstream_prompt_tokens` counter at `http://127.0.0.1:8080/fabric/stats`. With vLLM, also
read `usage.prompt_tokens_details.cached_tokens` per turn — the compile arm should keep
the compiled block as a stable prompt prefix, so it should show a higher cache hit ratio,
not just fewer tokens. Note which arm you changed: compiled prompts are smaller *only*
when the fabric is smaller than what the tool would otherwise paste in.

## Troubleshooting

- **502 `upstream unreachable`** — the model server is down or `--upstream` is wrong;
  check `http://127.0.0.1:8000/v1/models` directly.
- **No `X-Fabric-Compiled` header** — you are in `--mode passthrough`, or the request had
  no user message (those forward untouched).
- **No blocks appear in the packet** — blocks must pass the kernel's policy gate:
  `trust_domain` must equal `default`, `lifecycle_status` must be `routable`, and role
  restrictions (`allowed_roles`/`denied_roles`) must admit the `X-Fabric-Role` in use.
- **Edge cases** — streaming works: upstream SSE bytes are piped through unbuffered and
  unchanged, and `data:` lines carrying a `usage` object contribute to
  `upstream_prompt_tokens` best-effort. If the upstream dies mid-stream, the client hears
  `: fabric: upstream stream aborted` and `errors` increments (the sidecar never
  fabricates a `[DONE]`); a normal stream ends with the upstream's own `[DONE]`. The
  fabric is loaded at startup, and blocks written through `POST /fabric/blocks` are
  picked up immediately (JSONL files still need a restart to reload). Responses use
  HTTP/1.1 keep-alive; SSE responses set `Connection: close`.
