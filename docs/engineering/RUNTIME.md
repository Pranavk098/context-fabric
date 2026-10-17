# Stage B Runtime Recipe — vLLM + LMCache on the dev machine

**Goal:** stand up an OpenAI-compatible endpoint with prefix caching so `evals/spike_kv_cache.py`
can measure the real KV-reuse delta.

## 0. Hardware reality (measured 2026-10-16)

| Item | Value |
| :--- | :--- |
| GPU | NVIDIA RTX 5070 Laptop, **8 GB VRAM** |
| Driver | 617.14 (Windows) |
| WSL2 | Ubuntu 22.04 (installed, stopped) |
| Docker | installed, daemon currently stopped |

8 GB is **below** the design target (16–24 GB) and the GPU is Blackwell (sm_120), which
needs a recent CUDA/PyTorch stack. Consequences:

- Use a **small model**: `Qwen/Qwen2.5-1.5B-Instruct` (or 0.5B if VRAM is tight).
- Cap the context: `--max-model-len 4096`, `--gpu-memory-utilization 0.85`.
- vLLM is Linux-first — run it in **WSL2 or Docker**, not native Windows.

## 1. Preferred path: Docker (needs the daemon running)

```powershell
# Start Docker Desktop, then:
docker compose up -d vllm
docker compose logs -f vllm        # wait for "Application startup complete"
```

The compose file already enables `--enable-prefix-caching`. vLLM exposes cache telemetry
per request via `usage.prompt_tokens_details.cached_tokens` (what the harness reads) and
server-side counters via its `/metrics` endpoint (`vllm:gpu_prefix_cache_hit_rate`).

If the RTX 5070 (sm_120) is unsupported by the current image, either pull a newer
`vllm/vllm-openai` tag or use the WSL2 native path below.

## 2. Alternative path: WSL2 native

```bash
# inside WSL2 Ubuntu, with NVIDIA GPU passthrough working (nvidia-smi should list the GPU)
python3 -m venv ~/.venvs/vllm && source ~/.venvs/vllm/bin/activate
pip install --upgrade vllm
vllm serve Qwen/Qwen2.5-1.5B-Instruct \
  --enable-prefix-caching --max-model-len 4096 --gpu-memory-utilization 0.85 --port 8000
```

## 3. Run the spike

```powershell
$env:CF_BASE_URL = "http://localhost:8000/v1"
$env:CF_MODEL    = "Qwen/Qwen2.5-1.5B-Instruct"
uv run python -m evals.spike_kv_cache
```

Report lands in `evals/reports/kv_spike_*.json`.

## 4. Optional: LMCache (Tier-2 hardware path)

LMCache adds non-prefix KV reuse (CacheBlend) on top of vLLM's exact-prefix cache. It only
matters **after** the exact-prefix arm is measured — otherwise we cannot attribute the win.
Run the vLLM arm first, then enable LMCache (`LMCACHE_CONFIG_FILE`) and re-run to get the
delta. Details: https://docs.lmcache.ai (pin the version you use).

## 5. What we are measuring, and the Gate B decision

| Metric | Source | Why it matters |
| :--- | :--- | :--- |
| `prompt_tokens_total` | usage | billed input; the headline "N× reduction" |
| `cached_tokens_total` | `prompt_tokens_details` | proof the prefix cache actually engaged |
| `prefix_cache_hit_ratio` | derived | the Invariant-3 claim, measured |
| `prefix_continuity_ratio` | harness | append-only rendering holds across turns |
| `mean_ttft_ms` | harness | prefill cost proxy (→ VRAM/queue pressure) |

**Gate B:** if `cache_hit_ratio_CPP` is materially higher than `B0` **and**
`prompt_token_reduction` is significant **and** TTFT does not regress → the thesis holds on
this beachhead; proceed to Stage C. If not → pivot before building the router
(see `docs/CHANGES-FROM-PROPOSAL.md` §5).
