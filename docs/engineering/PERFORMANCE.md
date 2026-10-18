# Performance - measured budgets, not vibes

The compiler is a hot path (every agent turn goes through it), so it gets a numeric
budget and a gate. It does not get speculative rewrites: see "Rewrite only with a
profile" below.

## Measured (2026-10-17, dev box, Python 3.12 via uv)

Synthetic corpus: N `fact` blocks, each `content = "The deterministic router compiles a
context packet for the downstream agent. " * 4`, `token_estimate=60` precomputed,
consistent `content_hash`, `budget_base=4000`, no cartridge, no signer. Full render
included in the compile time, as `compile_packet` hashes the final packet.

| Candidate blocks | `compile_packet` mean | `render_packet` mean |
| ---: | ---: | ---: |
| 50 | 5.0 ms | (below clock resolution) |
| 200 | 13.7–14.3 ms | (below clock resolution) |
| 1000 | 61 ms (mean of 30 runs) | 0.01 ms (mean of 5) |

These are the post-λ-MMR numbers (the MMR similarity pass added the compile cost; the
pre-MMR means were 3.3 / 6.4 / 23.3 ms, still measured by the same test).
Rerun variance between sessions is a few percent (a fresh 2026-10-18 session measured
5.01 / 14.33 / 61.65 ms). Raw numbers are printed by
`tests/test_perf.py` (`uv run python -m pytest tests/test_perf.py -q -s`).

For scale: one warm-cache call to a 1.5B model on this box measured ~160-670 ms
(mean ~300 ms) in the Stage B KV spike (`evals/reports/kv_spike_20261016_142753.json`;
`docs/engineering/ENGINEERING-LOG.md` §10). Compiling 1000 candidate blocks costs
roughly a fifth of the mean model call we measured. The compiler is not the
latency bottleneck; the model is.

## The budget gate

`tests/test_perf.py` builds 1000 synthetic blocks and asserts:

- `compile_packet` mean over 30 runs **< 250 ms**,
- `render_packet` mean < **50 ms**.

The bounds are deliberately generous (the compile bound is roughly 4x the measured
mean; the render bound is looser still). CI machines are slower and noisier than the dev
box, and the goal is to catch order-of-magnitude regressions (an accidental O(n^2), a
tokenizer call per block in a loop), not to set speed records. The test uses no GPU, no
network, and no wall-clock assumptions; it runs in about a second. It also measures and
prints 50/200-block numbers for the record without asserting on them.

## Concurrency (offline harness)

`evals/bench_sidecar.py` (`uv run python -m evals.bench_sidecar`, ~30 s on the dev box)
starts the real sidecar in-process via `make_server` against an in-process fake
OpenAI-compatible upstream that answers instantly (canned JSON, no sleeps). N thread
clients (not async) are released together by a barrier and each sends M=20 sequential
`/v1/chat/completions` requests, for N in {1, 4, 16, 64}, in both passthrough and compile
mode. Compile mode runs against an in-memory SQLite store of 50 blocks with manifest
logging enabled, so the write lock is exercised; every request carries a unique objective
so no compile hits the manifest dedupe. Raw JSON lands in
`evals/reports/bench_sidecar_<ts>.json`; `tests/test_bench_sidecar.py` is a ~2 s smoke of
the harness (one N=4 cell), not the full matrix.

Because the upstream never sleeps, these numbers measure the **sidecar's own cost** (HTTP
handling, proxying, compile+render, manifest-log write) — not model speed — and they are
the worst case for the sidecar's share of end-to-end latency. Everything runs in one
process on one GIL.

Before hardening (2026-10-18, dev box, Python 3.12.13, Windows 11):

| mode | N | requests | req/s | p50 ms | p95 ms | p99 ms | errors |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| passthrough | 1 | 20 | 950.2 | 0.84 | 1.84 | 3.56 | 0 |
| passthrough | 4 | 80 | 965.7 | 3.51 | 5.99 | 23.12 | 0 |
| passthrough | 16 | 320 | 293.4 | 6.42 | 12.67 | 533.84 | 0 |
| passthrough | 64 | 1280 | 306.7 | 8.95 | 553.48 | 2057.14 | 23 |
| compile | 1 | 20 | 134.1 | 6.87 | 9.97 | 11.33 | 0 |
| compile | 4 | 80 | 134.5 | 29.25 | 40.61 | 45.38 | 0 |
| compile | 16 | 320 | 133.2 | 107.77 | 135.69 | 621.93 | 0 |
| compile | 64 | 1280 | 133.5 | 324.23 | 1371.73 | 2076.86 | 31 |

The N=1 delta (compile minus passthrough) was **+6.0 ms p50** (0.84 → 6.87 ms): the
sidecar's passthrough cost is under a millisecond per request, and 50-block compile+render
plus the manifest write adds ~6 ms — consistent with the 5.0 ms compile budget number
above.

### Hardening applied: listen backlog

At N=64 the unpatched server failed 23/1280 passthrough and 31/1280 compile requests, all
`ConnectError`, and showed ~0.5–2 s p95/p99 tails at N in {16, 64}. `socketserver`'s
default `request_queue_size` is **5**: a simultaneous >5-connection burst overflows the
accept queue (refused connections; Windows then retransmits SYN with its ~1 s initial RTO,
which explains the tails). `sidecar/server.py` now uses a
`_Server(ThreadingHTTPServer)` subclass with `request_queue_size = 256` — a small,
documented subclass, no behavior change otherwise. The other candidates were no-ops and
were *not* applied: `ThreadingHTTPServer` already sets `daemon_threads = True` and `HTTPServer`
already sets `allow_reuse_address = 1`; and nothing holds a lock during proxying — the
only `write_lock` sections are the O(50) snapshot copy and the manifest-log write, both
sub-millisecond, and the flat compile throughput below shows the compile ceiling is the
GIL, not the lock.

After (same harness, same box):

| mode | N | requests | req/s | p50 ms | p95 ms | p99 ms | errors |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| passthrough | 1 | 20 | 992.0 | 0.87 | 1.73 | 2.15 | 0 |
| passthrough | 4 | 80 | 899.5 | 4.09 | 6.46 | 10.53 | 0 |
| passthrough | 16 | 320 | 900.9 | 15.99 | 29.61 | 40.70 | 0 |
| passthrough | 64 | 1280 | 834.7 | 69.06 | 134.20 | 166.38 | 0 |
| compile | 1 | 20 | 109.6 | 6.84 | 25.50 | 31.36 | 0 |
| compile | 4 | 80 | 130.9 | 29.09 | 40.33 | 70.78 | 0 |
| compile | 16 | 320 | 133.0 | 118.32 | 149.94 | 162.66 | 0 |
| compile | 64 | 1280 | 126.8 | 497.35 | 557.35 | 754.40 | 0 |

Errors are gone everywhere and the second-scale tails collapsed: passthrough now holds
~900 req/s through N=16 and 835 req/s at N=64 (p99 166 ms, was 2.06 s). Compile stays
~130 req/s at every N with p50 growing ~linearly with N — that is the GIL, not the HTTP
layer: a 50-block compile is ~5 ms of CPU-bound Python, so threads only queue. Lifting
that ceiling needs process-level parallelism (e.g. several sidecars behind a load
balancer); not applied, because a real compile is ~5 ms against a ~300 ms model call, so
the sidecar is not the interactive bottleneck.

### What this does and does not tell us

- **Does**: bound the sidecar's own per-request cost (N=1: ~0.9 ms passthrough, ~7 ms
  compile at 50 blocks) and show the HTTP layer absorbs 64 simultaneous clients with zero
  errors and no multi-100-ms tails after the backlog fix.
- **Does not**: predict production capacity or end-to-end latency. It is a Windows dev
  box, one Python process, one GIL, and an artificial instant upstream; the in-memory
  store also hides the SQLite fsync that a disk-backed fabric adds per manifest. Run-to-run
  variance at N=1–4 is real (a parallel job on the box moved passthrough N=1 between 424
  and 992 req/s across runs); the N≥16 error/tail deltas before vs after are far outside
  that noise.
- At scale, model inference dominates and these harness numbers bound only the sidecar's
  additive cost — which, at ~1 ms passthrough or ~6 ms compile, is ~0.3–2% of the ~300 ms
  model call measured above.

## Rewrite only with a profile

No optimization merges on a hunch. If a budget is ever missed or latency becomes a
user-visible bottleneck:

1. Reproduce it in `tests/test_perf.py`'s synthetic corpus.
2. Profile it (`cProfile`, `py-spy`) and name the hot function with numbers.
3. Fix or rewrite **that** function only; re-run the full test suite and this gate.
4. Keep the previous Python implementation as the oracle until the new one is proven
   byte-identical on the test vectors.

## Language boundary rules

- **Core (protocol, kernel, evals) stays Python.** Determinism, auditability, and
  iteration speed matter more than raw throughput at this scale - 1000 blocks in ~61 ms
  is far inside an interactive budget.
- **UI would be TypeScript**, in its own process, talking to the sidecar over HTTP. No
  Python UI, no JS in the compiler.
- **Engine-side is the engine's language.** vLLM/LMCache integrations use whatever the
  engine exposes (Python plugins/connectors where available). Engine internals
  (CUDA/C++ kernels) are upstream's call, never ours to fork.
- **Hot loops** may move to a compiled extension or Rust/Rust-backed library only after
  a profile shows the Python hot spot dominating end-to-end latency. The Python path
  remains the correctness oracle and fallback.
