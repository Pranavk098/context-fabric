# Dogfood Run A7 — Context Fabric under a real agent tool (mini-swe-agent → vLLM → cfx serve)

Date: 2026-10-19 · Worker: wave-a7-dogfood · Hardware: RTX 5070 Laptop (8 GB VRAM), WSL2 Ubuntu 22.04

**Goal:** exercise `cfx serve` (compile mode + `--obs-fold`) under real OpenAI-compatible
agent-tool traffic and record every break. Task success was explicitly out of scope (a
1.5B local model is weak); the point is the traffic.

## 1. Setup that actually worked

Model: `Qwen/Qwen2.5-1.5B-Instruct` (3B did not fit in 8 GB alongside KV cache; 1.5B used).
Tool: `mini-swe-agent 2.4.6` (WSL `~/.venvs/swe`), `LitellmModel` pointed at the sidecar via
`OPENAI_API_BASE`/`OPENAI_API_KEY`, no OpenAI key. Fixture: a scratch copy of the httpx
repo (copied to `~/dogfood/httpx_scratch`; the original at
`C:\Users\<user>\AppData\Local\Temp\opencode\httpx_pilot` was never touched).

```bash
# vLLM (WSL) — three flags beyond the task's spec were REQUIRED, see bug S1:
source ~/.venvs/vllm/bin/activate
export VLLM_TORCH_COMPILE_LEVEL=0
export VLLM_USE_FLASHINFER_SAMPLER=0
vllm serve Qwen/Qwen2.5-1.5B-Instruct \
  --enable-prefix-caching --enable-auto-tool-choice \
  --max-model-len 16384 --gpu-memory-utilization 0.85 --port 8000 --enforce-eager

# cfx serve (WSL, the worktree's editable install: ~/.venvs/swe/bin/cfx)
cfx serve --upstream http://127.0.0.1:8000 --obs-fold --obs-store obs.db --port 8080

# mini-swe-agent (WSL), no OpenAI key:
export OPENAI_API_BASE=http://127.0.0.1:8080/v1 OPENAI_API_KEY=sk-dummy-no-openai
export MSWEA_COST_TRACKING=ignore_errors MSWEA_CONFIGURED=1 MSWEA_SILENT_STARTUP=1
cd ~/dogfood/httpx_scratch
mini-swe-agent -c <config> -m openai/Qwen/Qwen2.5-1.5B-Instruct -t "<task>" -y -l 0 -o traj.json
```

Note: the sidecar binds `127.0.0.1` only, so with WSL2 in NAT mode the sidecar and the
agent tool must run in the *same* OS (both in WSL here); a Windows-side sidecar is
unreachable from a WSL agent. The swe venv's `cfx` is an editable install of this worktree,
so the tested code is exactly the tree under review.

## 2. What worked end-to-end (protocol coverage)

| Path | Result |
| :--- | :--- |
| non-streaming tool-call relay (`tools=[bash]`) | ✅ byte-correct; structured `tool_calls` returned |
| streaming SSE relay (`stream=true`) | ✅ `text/event-stream`, chunks piped unchanged |
| streaming **tool-call deltas** | ✅ 7 delta chunks (`name` then `arguments` fragments) identical direct vs sidecar |
| compile-mode system injection | ✅ `X-Fabric-Compiled: 1` + `X-Fabric-Packet-Id` on every request with a user message |
| obs-fold (mask + window guard) | ✅ folds oldest `role:tool` messages into `ctx://obs/<id>` stubs |
| `cfx expand` reversibility | ✅ stub → full stored content; missing stub exits 1 |
| `cfx replay` on compile manifests | ✅ `REPLAY OK <packet_id> <content_hash>` |
| sidecar latency overhead | ✅ ~0 ms for small requests (direct 46.7 ms vs sidecar 42.3 ms mean over 3, noise) |

## 3. obs-fold under real traffic

With default `--obs-n 3 --obs-k 3`, obs-fold needs **6** tool messages before it folds the
oldest 3, and the 1.5B model could not sustain 6 tool calls in one session (it either
repeated-format-errored or wandered off). Lowering to `--obs-n 1 --obs-k 1 --obs-max-tokens 3000`
let obs-fold fire on real mini-swe-agent traffic: two distinct bash outputs were folded,
stored as `obs_<id>` blocks, and 7 `fold_log` events were written for session
`default-agent` (mini-swe-agent does not send `X-Fabric-Agent`, so everything lands in one
session). `seen_hash` changes per fold pass (it is the hash of that request's exact folded
message list); the blocks are deduplicated by content hash. **This confirms the fold path is
sound and the default threshold is the only reason it did not fire at default settings with a
weak model.**

One documented quirk confirmed: the `fold_log.message_index` is relative to the **post-compile**
message list — compile mode prepends its packet as a system message *before* obs-fold runs, so
real tool-message indices are offset by +1 (indices 3,5,7 became 4,6,8). Internally
consistent (replay hashes the post-compile list), but worth knowing when reconciling receipts.

## 4. Receipts & replay

- `cfx serve --fabric fabric.db` logs one `compile_log` manifest per (packet_id) — see §5 bug 3.
- `cfx replay --store fabric.db --packet-id cpk_…` recompiled and matched (`REPLAY OK`) on real
  manifests from a 2-block fabric.
- obs-fold receipts live in `fold_log` (append-only, `seen_hash`-linked); there is **no**
  `cfx replay` path for them — replay covers compile manifests only. Reversibility via
  `cfx expand` works.

## 5. Bugs & findings (numbered, with severity)

- **S1 — vLLM 0.31 needs `nvcc` (env, not CF).** Both the default torch.compile path and the
  flashinfer sampler JIT fail with
  `RuntimeError: Could not find nvcc and default cuda_home='/usr/local/cuda' doesn't exist`
  (torch 2.13.0+cu130, no CUDA toolkit installed). The sampler crash hit on the *first*
  top-k/top-p sample (i.e. any request not forced to temperature 0). Fix/workaround:
  `VLLM_TORCH_COMPILE_LEVEL=0 --enforce-eager VLLM_USE_FLASHINFER_SAMPLER=0`. Not a CF bug, but
  it will bite anyone following RUNTIME.md's plain `vllm serve …` recipe on this box.
- **S2 — `--tool-call-parser hermes` is wrong for Qwen2.5 (config, not CF).**
  `hermes_tool_parser.extract_tool_calls` throws
  `json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)` whenever the model
  emits text instead of native tool calls. Qwen2.5 emits OpenAI-format tool calls natively via
  its chat template, so the parser should be **omitted** — `--enable-auto-tool-choice` alone is
  enough. vLLM 0.31 registers no Qwen-specific parser (`qwen25`/`qwen25_native` are absent from
  `vllm/tool_parsers/__init__.py` at tag v0.31.0), and vLLM's tool-calling docs still list
  `--tool-call-parser hermes` under "Qwen Models"
  (https://docs.vllm.ai/en/stable/features/tool_calling/), so that recommendation is wrong on
  this box — "omit" is the correct option. This contributes directly to the model's "No tool
  calls found" failures under mini-swe-agent.
- **S3 (low) — `packet_id` is not content-addressed to the fabric.** Same objective + agent →
  same `packet_id` regardless of block set (0-block vs 2-block fabrics both returned
  `cpk_ddb6c803e553dc14`). Only `content_hash` distinguishes the content, and CD-06 keeps the
  first manifest per id. If blocks change mid-session with a stable objective, the new content
  is proxied but not re-receipted. Design decision, but surprising on first read.
- **S4 (low) — compile objective churns on agent-tool reminders.** `_objective()` takes the
  *last* user message; mini-swe-agent injects `"Tool call error: No tool calls found…"` as a
  user message on every failed turn, so the compiled packet's instruction (and packet_id)
  changes every turn. Heuristic weakness, not a correctness bug for the packet bytes.
- **S5 (low) — bare `GET /models` 404s.** Sidecar proxies `/v1/models` but not `/models`;
  both vLLM and sidecar return 404 (sidecar body `{"error":{"message":"not found"}}` vs vLLM's
  `{"detail":"Not Found"}`). Consistent, but a client that probes `/models` first sees a
  non-OpenAI error shape.
- **S6 (info) — default obs-fold threshold is too high for weak sub-agents.** `n=3,k=3` folds
  nothing below 6 tool messages; a 1.5B sub-agent rarely gets there, so the feature silently
  does nothing on the very traffic it targets. Consider exposing a lower default or documenting
  that weak agents should use `--obs-n 1 --obs-k 1`.

Nothing in this list was a clear, safe, single-line fix in a non-frozen file, so **no code was
changed** (obsfold.py is frozen; the server.py paths involved are either frozen or correct).

## 6. Cleanup confirmation

vLLM, both `cfx serve` instances, and the agent tool were stopped and GPU memory released
before this report (see the run log). No OpenAI/API calls were made; all traffic was local.
