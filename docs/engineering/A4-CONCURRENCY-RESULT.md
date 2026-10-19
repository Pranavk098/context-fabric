# A4 Concurrency Result — raw shared prefix vs compiled packet under N concurrent sub-agents

Pre-registration: `docs/engineering/A4-CONCURRENCY-PREREGISTRATION.md` (frozen
2026-10-19 22:56 UTC). Runs executed by an Orca-supervised OpenCode worker on the RTX 5070
Laptop 8 GB, one vLLM session per arm (restarted between arms), zero errors in every cell.

Receipts: `evals/reports/a4_raw_20261019_160616.json` (raw),
`evals/reports/a4_compile_20261019_160730.json` (compile),
`evals/reports/a4_dryrun_20261019_155534.json` (offline dry-run).

Setup: the 10-file httpx bundle (32,259 tiktoken, Qwen tokenizer ~32.2k) as the raw arm's
identical system message; the compile arm's packet = shared Tier-0 cartridge (595 tokens)
leading a per-agent packet over the mechanical fabric. `max_tokens=64`, `temperature=0.0`,
`--max-model-len 32768`, `--enable-prefix-caching`, `--enforce-eager`,
`--gpu-memory-utilization 0.85`. N ∈ {1, 4, 8, 16, 32} × 3 repeats, fresh cache tag per
repeat. Peak KV is the `vllm:kv_cache_usage_perc` fraction × 100 (the pilot's same gauge and
scale). Prompt tokens are the Qwen tokenizer from vLLM `usage.prompt_tokens`.

## Verdict

The pre-registered **primary (memory) claim PASSES at every N — no crossover**. Compile holds
less peak KV than raw at all five concurrency levels, from **10.8× less at N=1** to
**1.9× less at N=32**. The margin narrows with N but never flips.

The **secondary metrics** (reported, not gated) tell a more interesting story: compile wins on
throughput and p99 latency up to N=16, but **raw wins both at N=32** (throughput 5.44 vs 4.95
req/s; p99 5.88 s vs 6.45 s). The mechanism is queueing — see "What the crossover means".

## Numbers (aggregate of 3 repeats: mean for req/s·p50·p99, max for peak-KV·run·wait, sum for errors)

| arm | N | req/s | p50 ms | p99 ms | peak KV % | peak running | peak waiting | errors |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| raw | 1 | 0.21 | 4,745 | 4,745 | 25.88 | 1 | 0 | 0 |
| raw | 4 | 0.84 | 4,774 | 4,775 | 25.96 | 4 | 3 | 0 |
| raw | 8 | 1.63 | 4,898 | 4,899 | 26.06 | 8 | 7 | 0 |
| raw | 16 | 3.10 | 5,149 | 5,166 | 26.27 | 16 | 15 | 0 |
| raw | 32 | 5.44 | 5,824 | 5,875 | 26.68 | 32 | 31 | 0 |
| compile | 1 | 2.61 | 416 | 416 | 2.39 | 1 | 0 | 0 |
| compile | 4 | 4.47 | 863 | 892 | 8.09 | 4 | 2 | 0 |
| compile | 8 | 4.75 | 1,611 | 1,681 | 11.55 | 6 | 5 | 0 |
| compile | 16 | 4.92 | 2,530 | 3,248 | 12.19 | 7 | 13 | 0 |
| compile | 32 | 4.95 | 4,083 | 6,452 | 13.83 | 7 | 29 | 0 |

Ratios (compile ÷ raw, or raw ÷ compile where noted):

| N | peak KV (raw÷compile, × lower) | throughput (compile÷raw) | p99 (raw÷compile, × faster) |
|---:|---:|---:|---:|
| 1 | **10.8×** | 12.2× | 11.4× |
| 4 | **3.2×** | 5.3× | 5.4× |
| 8 | **2.3×** | 2.9× | 2.9× |
| 16 | **2.2×** | 1.6× | 1.6× |
| 32 | **1.9×** | 0.91× (raw wins) | 0.91× (raw wins) |

Prompt tokens per request (Qwen tokenizer, vLLM `usage`): raw **~32,230**, compile
**~2,956–2,976** → **~10.9× fewer**. Errors: **0** in all 30 cells; **0** context-length
rejections (the raw arm fit `--max-model-len 32768` as pre-registered, with 422 tokens of
tiktoken headroom).

## What the crossover means (the honest finding)

- **Peak KV (the memory claim) holds, and the mechanism is the one Context Fabric predicts.**
  Raw's peak KV is pinned at ~26% by its single 32.2k shared prefix (cached once, shared via
  copy-on-write), essentially flat across N. Compile's peak KV is ~2.4% at N=1 and grows to
  ~13.8% at N=32 as its per-agent ~3k packets accumulate — but it stays well below raw at
  every N, and the ~2× gap at N=32 survives.
- **But the memory win does not buy a throughput win at high N — it buys it at low-to-mid N,
  and raw's shared prefix buys it back at N=32.** Raw pays one ~4.6 s cold prefill of the 32k
  prefix per repeat, after which all N agents ride the cached prefix and decode together
  (peak running = N). Compile pays N distinct ~3k prefills, and under `--enforce-eager` the
  scheduler keeps only ~7 requests running at once (peak running saturates at 7, peak waiting
  reaches 29). So compile's throughput saturates near ~4.9 req/s while raw's scales to
  5.44 req/s at N=32, and compile's p99 tail latency (queueing) flips above raw's.
- **This is the strongest-naive-arm effect the pre-registration called out.** Raw's identical
  prefix is exactly what makes it hard to beat: it converts N agents' shared context into N
  concurrent decodes after one prefill. Compile wins on the metric it is designed to win
  (KV footprint) but, at the concurrency wall, trades that memory win for scheduler queueing.

## What holds and what does not

- **Holds — memory (the pre-registered primary).** Compile's peak KV is 1.9–10.8× lower than
  raw at every N, on the fixed `kv_cache_usage_perc` gauge the pilot used.
- **Holds — token volume.** ~10.9× fewer prompt tokens per request (Qwen tokenizer), and the
  shared-prefix structure is real: raw's 32.2k prefix is byte-identical across agents.
- **Does not hold — throughput at the wall.** At N=32 raw's throughput (5.44 req/s) exceeds
  compile's (4.95 req/s); compile's throughput saturates while raw's scales.
- **Does not hold — tail latency at the wall.** Compile's p99 is lower through N=16 but
  higher at N=32 (6.45 s vs 5.88 s), the price of queueing 29 requests behind ~7 running.

## Caveats

- The compile arm's peak running requests saturates at ~7 (never reaching N≥16); whether that
  is `--enforce-eager` prefill/decode batching, the scheduler's default, or something else is
  not isolated here. A follow-up (e.g. CUDA graphs, or a higher `--max-num-seqs`) would change
  the throughput/latency crossover point but not the pre-registered environment; reported, not
  re-run.
- Peak gauges are ~25 ms samples and can miss faster transients; running/waiting are
  independent peaks (max over samples), so `running + waiting` may exceed N and should be read
  as two separate maxima, not a simultaneous sum (the GAP-24 metric semantic).
- Compile's N=1 mean latency includes a cold first repeat (610 ms vs 321/316 ms); the engine
  warm-up precedes the arm but the first compile request still carries residual warm-up cost.
- Every repeat includes one cold prefill of the shared prefix (fresh cache tag), so these
  numbers are "spawn N fresh sub-agents" figures, not steady-state warm-cache throughput.
- Single GPU, one 1.5B model, one ~32k-token shared context, unscored objectives; the
  crossover magnitude is specific to this context size and scheduler config.
