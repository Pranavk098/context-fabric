# A4B Concurrency Explore — why compile saturates at ~7 running, and the serving-parameter sweep

**Status: EXPLORATORY.** Not a pre-registered claim. These runs change vLLM serving
parameters from the A4 baseline to *find* a setting, not to *confirm* one. Numbers here are
descriptive, 2-repeat sweeps on one GPU; treat magnitudes as directional. **No setting removed
the crossover (§4), so no confirmatory pre-registration was produced** — there is nothing to
pin and re-run.

Setup is inherited verbatim from A4 (`docs/engineering/A4-CONCURRENCY-PREREGISTRATION.md`):
same fixture (10-file httpx bundle, 32,259 tiktoken), same 32 objectives, same two arms
(raw = identical 32k shared prefix; compile = shared 595-token Tier-0 cartridge + per-agent
~2.3k divergent packet), same metrics and gauges, `max_tokens=64`, `temperature=0.0`,
`--max-model-len 32768`, `--enable-prefix-caching`, `--enforce-eager`,
`--gpu-memory-utilization 0.85`, one vLLM session per arm. Only the serving parameters in
§2 differ, and both arms always get the **same** parameters (fairness constraint).

## 1. Diagnosis: why compile's running requests saturate at ~7

A4 observed `peak_requests_running = 7` for the compile arm at every N ≥ 16 (vs `= N` for
raw), with `peak_requests_waiting` reaching 29 at N=32. The binding constraint is vLLM's
per-step **token budget**, not sequence count and not KV memory:

- **The default `--max-num-batched-tokens` is 2048** (vLLM 0.31.0,
  `SchedulerConfig.DEFAULT_MAX_NUM_BATCHED_TOKENS`). `max_num_seqs` defaults to **128**,
  chunked prefill defaults **on**. Confirmed from the installed package
  (`~/.venvs/vllm/.../vllm/config/scheduler.py`) and from the startup log
  (`enable_chunked_prefill=True`, V1 engine v0.31.0).
- **Compile's per-request divergent prefill is ~2,315 tokens**, larger than the 2,048-token
  budget. The dry-run packet is 2,885–2,930 tiktoken per request (mean 2,910); subtract the
  595-token shared cartridge and each agent has ~2,315 tokens of *divergent* prefill. Since
  2,315 > 2,048, a single request's divergent prefill does not fit one scheduler step, so
  chunked prefill can admit only ~1 divergent prefill per step.
- **Live metrics agree.** During a compile N=32 burst, `vllm:num_requests_running` maxes at
  **7**, `vllm:num_requests_waiting_by_reason{reason="capacity"}` climbs to **29**,
  `vllm:num_preemptions_total` stays **0**, and `vllm:kv_cache_usage_perc` peaks at ~**13.8%**.
  Requests wait on scheduler **capacity** (token budget), not on KV blocks (13.8% ≪ 100%, zero
  preemptions) and not on sequence slots (7 ≪ 128).
- **Raw is not affected the same way.** Raw pays one ~32k cold prefill (chunked into 2,048-token
  steps) per repeat, after which all N agents ride the cached prefix and decode together —
  1 token/step each — so raw's peak running scales to N. Raw is prefill-bound on *one shared
  prefix*; compile is prefill-bound on *N divergent ~2.3k prefixes* that serialize at a
  2,048-token budget.

Conclusion of the diagnosis: `--max-num-batched-tokens 2048` (the default) is the crossover
mechanism. The sweep raises it (and varies `max_num_seqs` / chunked prefill) to see whether a
fair setting removes compile's saturation.

## 2. Swept serving configs

Every config shares the A4 base flags; only the listed extras differ:

| label | extra `vllm serve` flags | intent |
| :--- | :--- | :--- |
| `baseline` | *(none)* | reproduce A4 defaults (batched 2048, seqs 128, chunked on) |
| `b8192` | `--max-num-batched-tokens 8192` | ~3.5 concurrent divergent prefills |
| `b16384` | `--max-num-batched-tokens 16384` | ~7 concurrent divergent prefills |
| `b32768` | `--max-num-batched-tokens 32768` | ~14 concurrent divergent prefills |
| `b8192s256` | `--max-num-batched-tokens 8192 --max-num-seqs 256` | isolate the seqs knob |
| `nochunk` | `--no-enable-chunked-prefill --max-num-batched-tokens 32768` | monolithic prefill at full budget |

## 3. Results (2 repeats, mean of req/s·p50·p99, max of peak-KV·run·wait, sum of errors)

All 12 arm-runs completed with **zero errors** in every cell (no `context_length`, no
`client_error`). `errors = 0` throughout, so the error column is omitted.

| config | arm | N | req/s | p50 ms | p99 ms | peak KV % | peak run | peak wait |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | raw | 16 | 3.10 | 5,141 | 5,144 | 0.26 | 16 | 15 |
| baseline | raw | 32 | 5.65 | 5,598 | 5,645 | 0.27 | 32 | 31 |
| baseline | compile | 16 | 4.68 | 2,538 | 3,417 | 0.12 | 7 | 13 |
| baseline | compile | 32 | 4.89 | 4,131 | 6,535 | 0.14 | 7 | 29 |
| b8192 | raw | 16 | 3.00 | 5,312 | 5,316 | 0.30 | 16 | 15 |
| b8192 | raw | 32 | 5.50 | 5,746 | 5,805 | 0.30 | 32 | 18 |
| b8192 | compile | 16 | 4.60 | 3,444 | 3,476 | 0.35 | 16 | 8 |
| b8192 | compile | 32 | 4.99 | 6,337 | 6,411 | 0.47 | 22 | 24 |
| b16384 | raw | 16 | 3.02 | 5,273 | 5,278 | 0.36 | 16 | 0 |
| b16384 | raw | 32 | 5.87 | 5,392 | 5,435 | 0.36 | 32 | 0 |
| b16384 | compile | 16 | 4.65 | 3,415 | 3,432 | 0.42 | 16 | 6 |
| b16384 | compile | 32 | 5.04 | 6,302 | 6,342 | 0.84 | 32 | 18 |
| b32768 | raw | 16 | 3.00 | 5,307 | 5,311 | 0.61 | 16 | 0 |
| b32768 | raw | 32 | 5.75 | 5,512 | 5,554 | 0.62 | 32 | 0 |
| b32768 | compile | 16 | 4.81 | 3,317 | 3,324 | 0.72 | 16 | 1 |
| b32768 | compile | 32 | 5.04 | 4,537 | 6,343 | 0.98 | 22 | 12 |
| b8192s256 | raw | 16 | 3.07 | 5,207 | 5,210 | 0.30 | 16 | 15 |
| b8192s256 | raw | 32 | 5.73 | 5,522 | 5,570 | 0.30 | 32 | 19 |
| b8192s256 | compile | 16 | 4.70 | 3,367 | 3,397 | 0.35 | 16 | 11 |
| b8192s256 | compile | 32 | 5.07 | 6,229 | 6,305 | 0.47 | 22 | 24 |
| nochunk | raw | 16 | 2.96 | 5,390 | 5,395 | 0.61 | 16 | 0 |
| nochunk | raw | 32 | 5.66 | 5,598 | 5,640 | 0.62 | 32 | 0 |
| nochunk | compile | 16 | 4.67 | 3,406 | 3,421 | 0.72 | 16 | 0 |
| nochunk | compile | 32 | 3.25 | 7,973 | 9,839 | 0.98 | 22 | 12 |

The N=32 crossover, per config (the metric that flips in A4):

| config | raw req/s | compile req/s | compile ÷ raw | N=32 winner |
| :--- | ---: | ---: | ---: | :--- |
| baseline | 5.65 | 4.89 | 0.87 | raw |
| b8192 | 5.50 | 4.99 | 0.91 | raw |
| b16384 | 5.87 | 5.04 | 0.86 | raw |
| b32768 | 5.75 | 5.04 | 0.88 | raw |
| b8192s256 | 5.73 | 5.07 | 0.88 | raw |
| nochunk | 5.66 | 3.25 | 0.57 | raw (worst) |

And the peak-KV (memory) comparison at N=32 — the A4 *primary* claim:

| config | raw KV % | compile KV % | compile ÷ raw | compile wins KV? |
| :--- | ---: | ---: | ---: | :--- |
| baseline | 0.27 | 0.14 | 0.52 | **yes (1.9× lower)** |
| b8192 | 0.30 | 0.47 | 1.57 | no |
| b16384 | 0.36 | 0.84 | 2.33 | no |
| b32768 | 0.62 | 0.98 | 1.58 | no |
| b8192s256 | 0.30 | 0.47 | 1.57 | no |
| nochunk | 0.62 | 0.98 | 1.58 | no |

## 4. Does any setting remove the crossover?

**No.** Every config leaves raw winning throughput at N=32 (compile ÷ raw = 0.57–0.91), while
compile still wins at N=16 (compile ≈ 4.6–4.8 req/s vs raw ≈ 3.0). The crossover point does not
move. Two things fall out of the sweep:

1. **Raising the token budget lifts running but not throughput.** `--max-num-batched-tokens`
   2048 → 8192 raises compile's peak running 7 → 22 and 8192 → 16384 raises it to 32, but
   compile N=32 throughput barely moves (4.89 → 5.04 req/s, +3%) and p50 *worsens* (4.1 s →
   6.3 s). The GPU is already saturated doing the ~74k divergent prefill tokens at N=32; the
   scheduler cap was not the real ceiling.
2. **Raising the budget destroys compile's memory advantage.** Compile peak KV at N=32 grows
   0.14 → 0.47 → 0.84 → 0.98 as the budget rises, so at every config except the default,
   compile uses *more* peak KV than raw (raw stays ~0.27–0.62). The default 2,048-token budget
   is *protective*: it serializes prefill, which keeps compile's peak KV low — the very thing
   the memory claim measures.

The crossover is therefore **fundamental to the workload, not a tuning artifact**. Compile
prefills `595 + N·~2,315` tokens (≈ 74k at N=32) of *divergent* content, while raw prefills one
shared 32k prefix once and then decodes N agents in parallel. Those two curves cross near
N ≈ 14; above it, compile does strictly more prefill work than raw, and no fair scheduler
setting changes that. `--max-num-seqs` (128 → 256, `b8192s256`) is irrelevant (peak run never
approaches 128), and disabling chunked prefill (`nochunk`) makes compile strictly worse
(throughput 3.25 req/s, p99 9.8 s) while also breaking its KV advantage.

The honest recommendation: keep the A4 defaults. The scheduler is already doing the best
available trade — low budget to protect compile's peak KV, accepting a throughput/latency
crossover at N=32 as the price of the memory claim. A real fix for the crossover would have to
reduce compile's *divergent* prefill volume (e.g. a larger shared prefix), which is a change to
the packet structure, not a serving parameter.

Receipts: `evals/reports/a4b_<arm>_<config>_<ts>.json`. Runner: `scripts/wsl_run_a4b.sh`;
eval: `evals/a4b_concurrency.py`.
