# Engine matrix — `cfx demo` beyond vLLM

The vLLM numbers elsewhere in this repo are one engine's numbers. This doc records what was
actually run to test portability, what transferred, and what did not. Reference engine: vLLM
0.31.0 (WSL2). Portability target: **Ollama 0.19.0, Windows-native** — a genuinely different
serving stack (no PagedAttention, no Prometheus `/metrics`, different quantization, different
default context handling). Every figure below cites a receipt under `evals/reports/`.

## What was tested

| | vLLM (reference) | Ollama (this run) |
|---|---|---|
| Version | 0.31.0 | 0.19.0 (client + server) |
| Host | WSL2 | Windows-native |
| Serving flags | `--enforce-eager --enable-prefix-caching --max-model-len 8192` | `OLLAMA_CONTEXT_LENGTH=8192` (see quirk 1) |
| Model | `Qwen/Qwen2.5-1.5B-Instruct` (fp16) | `qwen2.5:1.5b` (Q4_K_M, 986 MB; manifest `65ec06548149`, config digest `sha256:377ac4d7aeefd5b870c9fccff9a6d4df36901d99fe3277c2f755bc401601ba1c`) |
| Endpoint | `http://127.0.0.1:8000` | `http://127.0.0.1:11434` |
| Fleet receipt | `evals/reports/demo_fleet_20261017_231929.json` | `evals/reports/ollama_fleet_20261018_100707.json` |
| Negative receipt | `evals/reports/demo_negative_20261018_101301.json` | `evals/reports/ollama_negative_20261018_100740.json` |

Scenario, fixture, objectives, scorer, and sampling parameters (`temperature=0.0`,
`max_tokens=96`, non-streaming) are identical by construction — the same `cfx demo` command
ran against both endpoints. No code was modified for the Ollama run. Both engines served
with an 8192-token window, so the ~7.5k-token broadcast prompt fits in both (this equality
matters; see quirk 1). A 4k-context smoke run (`ollama_fleet_smoke_20261018_100551.json`)
is kept as evidence of the truncation hazard, not as a comparison point.

Reproduce:

```bash
# Windows PowerShell; GPU must be free (no vLLM in WSL, checked with nvidia-smi)
ollama pull qwen2.5:1.5b
$env:OLLAMA_CONTEXT_LENGTH="8192"; ollama serve
uv run cfx demo --scenario fleet --upstream http://127.0.0.1:11434 --model qwen2.5:1.5b \
  --out evals/reports/ollama_fleet_<ts>.json
```

`--upstream` is the host base **without** `/v1` (the sidecar appends `/v1/chat/completions`,
`sidecar/server.py:291`); `http://127.0.0.1:11434/v1` yields a 404 — same convention as the
vLLM setup, easy to get wrong.

## Fleet results — token reduction is engine-independent

| arm | engine | prompt tok | per req | completion | accuracy | wall | mean lat |
|---|---|---:|---:|---:|---:|---:|---:|
| broadcast | vLLM | 44,854 | 7,476 | 188 | 6/6 | 2.80 s | 465 ms |
| compile | vLLM | 14,170 | 2,362 | 139 | 6/6 | 2.08 s | 346 ms |
| broadcast | Ollama | 44,854 | 7,476 | 186 | 6/6 | 10.92 s* | 1,818 ms |
| compile | Ollama | 14,170 | 2,362 | 103 | 6/6 | 2.11 s | 352 ms |

\* Ollama's broadcast wall includes cold model load (the first request of the run); the
compile arm ran warm. Mean-latency comparison broadcast 1,818 ms vs compile 352 ms reflects
Ollama's slower prefill of the ~7.5k-token blob, not a protocol difference.

All four prompt-token totals are **byte-identical across engines** (44,854 / 14,170 fleet;
4,884 / 5,022 negative — see below). That is the strongest available evidence for the core
claim: prompt text is rendered by the sidecar *before* the engine sees anything, and the
engine only tokenizes it. The 3.17x reduction (30,684 tokens saved) is a property of the
compile pipeline, not of vLLM.

Accuracy parity (6/6 both arms, both engines) is a deterministic substring check on the
same fixture; completions differ slightly (186 vs 188, 103 vs 139 completion tokens) because
Ollama serves a Q4_K_M quantization, not bit-identical weights. This is expected and does
not affect the token claim: token counts are computed from the *prompt*, which is identical.

Also verified engine-independent: the `x-fabric-compiled` header (6 `true` / 6 `false` per
receipt, recorded as `compiled` on every chat record), the quarantine of the draft block
(compiled packets deny `lifecycle:draft` in the sidecar's render path — the packet text that
tokenizes to 14,170 on both engines is the same policy-filtered text), the deterministic
scorer, and the real write path.

## Negative scenario (Ollama)

Pilot froze the same trap as the vLLM 1.5B run (`cache`, control pilot 2/2); dead end written
through the real write path (`POST /fabric/blocks` → 201, `blk_43ad7cf08bcc0814`, seq 27).

| arm | v1 bad | v2 bad | v3 bad | prompt tok | completion |
|---|---:|---:|---:|---:|---:|
| control | 2/2 | 2/2 | 2/2 | 4,884 | 192 |
| treated | 2/2 | 0/2 | 2/2 | 5,022 | 192 |

Control-vs-treated separation under the pre-registered v2 scorer replicates the vLLM 1.5B
result (v2 flags control and clears treated: 2/2 → 0/2 in this Ollama run, 1/2 → 0/2 in the
fresh vLLM `_101301` set — one control completion became the documented bullet style); prompt
totals again match the vLLM run exactly (4,884 / 5,022). The
treated v3 hits are known scorer classes, not a portability failure: one response leads a
bullet with "**Redis or Memcached**: These are not recommended…" (v3's documented
bullet-initial bias/anaphora limit, `NEGATIVE-SCORER.md` §4). Scorer verdicts are computed
in-process from the response text and cannot depend on the engine; as always, treat n=2 and
the reported counts as raw, not statistical (see `NEGATIVE-SCORER.md`, `DEMO.md`).

## What transfers, what does not

| claim | vLLM | Ollama | evidence |
|---|---|---|---|
| Prompt-token reduction (3.17x) | yes | yes | identical 44,854/14,170 |
| Packet compilation + draft quarantine | yes | yes | identical prompt tokens; enforce in sidecar render |
| Deterministic scorer, write path, pilot protocol | yes | yes | same in-process code, replicated receipt shape |
| `x-fabric-compiled` per-response signal | yes | yes | header observed on all 12 fleet records |
| Prefix-cache counters (`prefix_cache_hits/queries`) | yes | **no** | Ollama exposes no Prometheus `/metrics`; receipt `cache.available=false`, never faked |
| KV telemetry (`kv_cache_usage_perc` sampling) | yes | **no** | same |
| Cached-token cost estimate (`--price`) | partial (vLLM 0.31 leaves `usage…cached_tokens` null) | **no** — all input billed at full price, assumptions say so | `DEMO.md` §Costs |
| Prefix-cache / KV-sharing semantics (fold/fork, cache hints) | measured (`stage_b5` artifacts) | **unverified** | not exercised by the demo; Ollama does cache prompts internally but exposes no counters, so it cannot be measured |

## Ollama-specific notes (observed)

1. **Default context silently truncates.** On this 8 GB GPU, Ollama's default context is
   4k. With the default, every broadcast request was truncated to exactly 4,096 prompt
   tokens (front of the system message dropped — the fixture facts were lost), broadcast
   accuracy collapsed to 0/6, and the run *looked* like a token win while measuring garbage.
   `OLLAMA_CONTEXT_LENGTH=8192` fixed it and produced exact vLLM parity. Any cross-engine
   token comparison must pin the context length on both sides.
2. **`usage.prompt_tokens` is trustworthy** on the OpenAI-compat endpoint and matched
   vLLM's counts exactly on all four prompt sets (same Qwen tokenizer/template family).
3. **No `/metrics`.** The demo's cache section degrades gracefully by design: `available:
   false`, counters `null`, and the stdout line `cache: unavailable (upstream has no
   /metrics) - counters skipped`. Nothing is faked; `--price` would bill all input at full
   price with an explicit assumption line.
4. **Model load lands in the first arm's wall clock.** Pin/warm-up before timed comparisons;
   the receipt's `started_at` and per-request latencies make this auditable.
5. **Quantization gap.** `qwen2.5:1.5b` is Q4_K_M, not the fp16 HF checkpoint. Substring
   accuracy parity replicated here, but do not expect identical completions or use this
   artifact for sampling-behavior claims.

## Gaps (honest)

- No SGLang, llama.cpp/llama-server, TGI, or LM Studio runs yet; this doc covers two engines.
- Cache *semantics* (prefix sharing, KV reuse, hint alignment) are unverified for any
  non-vLLM engine — Ollama has no counters, so it cannot be measured with current tooling.
- The negative arm is n=2 per arm, one trap, one quantization; engine portability of the
  *pipeline* is shown, not of any behavioral effect.
- Ollama was run Windows-native against the same machine that runs vLLM in WSL2; the two
  were not run concurrently (GPU arbitration by hand; `nvidia-smi` + `wsl -- pgrep -f vllm`).
- Fleet artifacts were produced from a working tree with in-flight edits by a concurrent
  agent (`sidecar/costs.py`, scorer v3 wiring). The fleet fixture and prompt rendering were
  untouched: token parity with the 2026-10-17 set proves the sent prompts are identical.
