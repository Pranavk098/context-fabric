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
