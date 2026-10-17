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
counters (requests, compiled, errors, and summed upstream `prompt_tokens` from
non-streaming responses) for a quick before/after token read; `/fabric/health` is a
liveness probe.

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
uv run python -m sidecar --fabric fabric.jsonl

# 5. point your tool at http://127.0.0.1:8080/v1 (any API key; it is forwarded upstream)
#    check: Invoke-RestMethod http://127.0.0.1:8080/fabric/health
```

**WSL2:** vLLM runs inside WSL2, but WSL2 forwards `localhost` to Windows, so the
Windows-side sidecar reaches it at `http://127.0.0.1:8000` with no extra configuration. If
that fails, use the WSL IP (`wsl hostname -I`) as `--upstream`.

CLI flags (env fallbacks): `--upstream`/`CF_UPSTREAM`, `--port`/`CF_PORT`,
`--fabric`/`CF_FABRIC`, `--mode`/`CF_MODE`. `X-Fabric-Agent` and `X-Fabric-Role` request
headers optionally set the packet recipient (defaults: `default-agent` / `user`).

## Honest A/B recipe

Run the **same multi-turn task** twice against the same model and settings, restarting the
sidecar between arms:

```powershell
# arm 0 — control
uv run python -m sidecar --mode passthrough --fabric fabric.jsonl
# arm 1 — treatment
uv run python -m sidecar --mode compile --fabric fabric.jsonl
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
- **Edge cases** — streaming works: upstream SSE bytes are piped through unbuffered, but
  streaming responses never contribute to `upstream_prompt_tokens` (only non-streaming
  JSON `usage` is parsed). The fabric file is loaded once at startup; restart to reload it.
