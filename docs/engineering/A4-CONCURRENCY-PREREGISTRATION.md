# A4 Pre-Registration — concurrent sub-agents: raw shared prefix vs compiled packet

**Status: PRE-REGISTERED. Frozen 2026-10-19 22:56 UTC, before any model request or vLLM run
against this fixture.** The only thing run before this freeze is the **offline dry-run** (§10):
`uv run python -m evals.a4_concurrency --dry-run` — it reads the fixture bytes and builds both
arms' prompts (compile via the shipped `compile_packet` + `render_packet`, no model, no
network, no vLLM) to produce the token table and the raw-fit check. Everything below (fixture,
objectives, arms, metrics, comparison rule, error counting, max context length) is fixed now;
deviations are allowed only under the anti-tuning rule (§9). This document is the binding
specification for `evals/a4_concurrency.py`.

## 1. Question

At **N concurrent sub-agents** (N ∈ {1, 4, 8, 16, 32}) on one RTX 5070 8 GB with vLLM prefix
caching and `Qwen2.5-1.5B-Instruct`, how do **throughput (req/s)**, **p50/p99 latency**,
**queueing** (vLLM `num_requests_running` / `num_requests_waiting` gauges), **peak KV usage**,
and **request errors** compare between two arms:

- **R — raw**: every agent gets the full shared repo context as an **identical** system message
  (the strongest naive arm: the prefix is identical across agents, so vLLM caches it once and
  the N agents share it via copy-on-write).
- **C — compile**: per-agent compiled packet with the **shared Tier-0 cartridge first** (the
  cartridge hologram leads every packet, so the N agents share that prefix; the rest of each
  packet — per-agent instruction + recalled blocks — diverges per objective).

This is an engineering falsification run on real bytes, not a benchmark claim. The prior Stage
B5/F6 work measured compile against an *unshared* raw broadcast (20× KV) and a *small*
synthetic shared prefix (~2× KV). A4 is the harder, larger, and more realistic test: a real
~32k-token shared prefix, and per-agent objectives that force each compiled packet to diverge
beyond the cartridge. The honest prior expectation is a **crossover**: compile wins on peak KV
at low N (where the raw 32k prefix dominates a few agents), and the raw shared prefix amortizes
at high N (where compile's per-agent ~2.3k divergent packets accumulate).

## 2. Fixture (fixed now)

- Repo: `https://github.com/encode/httpx`, the pinned pilot commit
  `b5addb64f0161ff6bfe94c124ef76f6a1fba5254` (2026-02-23).
- Clone path used for the run:
  `C:\Users\<user>\AppData\Local\Temp\opencode\httpx_pilot`
  (WSL: `/mnt/c/Users/<user>/AppData/Local/Temp/opencode/httpx_pilot`).
- Bundle rule: the same 10-file bundle as the pilot — the `httpx/` package `.py` files in
  sorted relative-path order, first 10 (the smallest sorted prefix ≥ 30k tokens). Exact file
  list (in order): `httpx/__init__.py`, `httpx/__version__.py`, `httpx/_api.py`,
  `httpx/_auth.py`, `httpx/_client.py`, `httpx/_config.py`, `httpx/_content.py`,
  `httpx/_decoders.py`, `httpx/_exceptions.py`, `httpx/_main.py`.
- Rendering (deterministic, the only transformation): raw UTF-8 bytes in listed order, one
  blank line between files, each file prefixed by `===== FILE: <path> =====`. The rendered
  bundle is the raw arm's system message. **Measured pre-run: 32,259 tiktoken (o200k).**
- The fixture manifest at `evals/fixtures/pilot_fixture_20261018_151640.json` records the
  per-file sha256 so a fresh clone at the same commit can audit the exact bytes.

## 3. Objectives (fixed now; 32, one per sub-agent)

Fixed set of 32 per-agent objectives, agent `i` uses objective `i`. Uniform template over a
frozen symbol list, so the only per-agent variation is the symbol (identical tail structure
across agents — a controlled benchmark, and each symbol is a real top-level name of the bundle,
so the raw arm's context contains the answer and the mechanical fabric's recall has something
concrete to match). The symbols, in order:

`Client, AsyncClient, BaseClient, Timeout, Limits, Proxy, request, stream, get, post, head,
put, patch, delete, Auth, BasicAuth, DigestAuth, FunctionAuth, NetRCAuth, GZipDecoder,
DeflateDecoder, BrotliDecoder, ZStandardDecoder, HTTPError, RequestError, TimeoutException,
TooManyRedirects, HTTPStatusError, InvalidURL, encode_json, encode_request, main`.

Objective `i` (sent verbatim as the user message):

> In the httpx repository, which file defines `<symbol>`? Answer with the file path only.

Build-time assertion (in `evals/a4_concurrency.py`, before any request): every symbol must be
present (case-insensitive) in the rendered raw bundle; a missing symbol aborts the run (a
fixture/content error, not a result). **Accuracy is not measured and is not a criterion**: the
objective is the per-agent tail content that drives the compiled packet's divergence, not a
scored task.

## 4. Arms (fixed now)

Both arms send `[{"role":"system", …}, {"role":"user", <objective>}]` with the objective as the
user message; only the system content differs.

- **raw**: system = `<tag> + rendered bundle`. Identical system message for all N agents (only
  the user objective differs), maximizing upstream prefix-cache reuse.
- **compile**: system = `<tag> + rendered packet`. The packet is built with the shipped
  `compile_packet(cartridge=…, blocks=…, budget_base=4000, as_of=DEFAULT_AS_OF)` over the
  mechanical fabric (`evals.mech_fabric.build_blocks`, 145 blocks, with the `blk_cartridge`
  artifact block removed to avoid duplicating the pinned cartridge) and rendered with
  `sidecar.render.render_packet`. The Tier-0 cartridge hologram leads, so the shared prefix is
  the cartridge; the per-agent instruction (which carries the objective) and recalled blocks
  follow and diverge. The objective also appears in the instruction (packet) and in the user
  message — byte-for-byte the same structure the pilot's compile arm used.

**Cache tag** (both arms): `A4::{arm}::{N}::{repeat}::` is prepended to the system message.
Within a repeat all N agents share the tag (and thus the shared prefix); a fresh tag per repeat
means vLLM's persistent prefix cache cannot carry between repeats.

**Order and isolation**: `--arm raw` and `--arm compile` are separate invocations, and **vLLM
is restarted between arms** (two fresh vLLM sessions), so no prefix-cache state carries between
raw and compile. Within an arm, N levels run in ascending order (1, 4, 8, 16, 32); each level
is 3 independent bursts with fresh tags.

## 5. Measurement (fixed now)

Per burst (one arm × N × repeat): fire N requests concurrently (`asyncio.gather` over one
`httpx.AsyncClient`, non-streaming, `temperature=0.0`, `max_tokens=64`) and record, per request:
HTTP status, latency (wall-to-completion, ms), prompt/completion tokens, and an error kind. Per
burst: wall time, throughput (`N / wall_s`), p50/p99 latency over the N request latencies
(nearest-rank), and the peaks of the vLLM gauges `vllm:kv_cache_usage_perc`,
`vllm:num_requests_running`, `vllm:num_requests_waiting` sampled at ~25 ms. `max_tokens=64` and
`temperature=0.0` are fixed.

Per cell (arm × N): aggregate over the 3 repeats as follows — `peak_kv` = **max** across
repeats (worst case, conservative for memory); `req/s`, `p50`, `p99` = **mean** across repeats;
`peak_waiting` = **max** across repeats; `errors` = **sum** across repeats.

The first-scheduled request in each burst cold-prefills the shared prefix (32k for raw, ~595
tokens for compile) while the rest ride the copy-on-write cache. Each repeat is therefore a
complete, independent "spawn N fresh sub-agents" trial; no per-repeat warm-up is sent, and the
cold prefill is part of what is measured.

## 6. Comparison rule (fixed now)

Aggregated per (arm, N). For each metric the comparison is made at **each N**, and the headline
is the **largest clean N** (N=32 target; see §7 for the validity gate and fallback).

- **Peak KV usage (primary — the memory claim).** At N, compile WINS iff
  `C.peak_kv < R.peak_kv`; report `R.peak_kv / C.peak_kv` as "× lower KV" (values < 1.00 mean
  compile is worse). The **crossover N** is the smallest N where `C.peak_kv ≥ R.peak_kv`
  (within 0.01 percentage-point rounding, treated as a tie). Headline verdict at the largest
  clean N: compile WINS iff `C.peak_kv < R.peak_kv`, otherwise LOSES (or TIES).
- **Throughput (req/s).** At N, compile WINS iff `C.req_s > R.req_s`; report `C.req_s / R.req_s`.
- **Latency.** At N, compile WINS iff `C.p99 < R.p99`; p50 reported alongside.
- **Queueing.** At N, compile WINS iff `C.peak_waiting < R.peak_waiting`; peak running reported
  alongside.
- **Errors.** Reported per cell (count and kind); never a "win". See §7.

Throughput, latency, and queueing are reported descriptively; the pre-registered success claim
is the **memory** one (peak KV). There is no single aggregate PASS/KILL: the verdict is the
per-N table plus the crossover N, reported as-is.

## 7. Error counting and validity gate (fixed now)

Each request is classified into exactly one of:

| kind | definition |
|---|---|
| `ok` | HTTP 200 |
| `client_error` | no HTTP response (httpx transport exception: timeout, connection, …) |
| `context_length` | non-200 whose body matches one of the frozen markers (§7a) |
| `http_error` | non-200 not matching a context-length marker |

**§7a — context-length markers (frozen):** `maximum context length`, `maximum model length`,
`maximum sequence length`, `prompt length`, `context length`, `input is too long`,
`model's maximum`, `exceeds the model`, `exceeds maximum` (case-insensitive substring). A
`context_length` rejection means the prompt did not fit the served window.

**Validity gate:** a cell (arm, N) is **INVALID** iff `errors > 25%` of its `3N` requests, **or**
any request is `context_length`. An INVALID cell is reported in full but excluded from the
headline verdict; the headline then falls back to the largest clean N, stated explicitly. A run
where every N > 1 cell of either arm is INVALID is reported as **INCONCLUSIVE**.

## 8. Max context length and raw-arm fit (fixed now)

Served window: `--max-model-len 32768` (the model's native 32k window). The raw arm fits:

| term | tokens (tiktoken o200k) |
|---|---:|
| rendered bundle | 32,259 |
| longest objective | 23 |
| `max_tokens` | 64 |
| **total (bundle + objective + max_tokens)** | **32,346** |
| headroom to 32,768 | 422 |

The Qwen2.5 chat template adds a handful of tokens (measured at run time); the pilot already
served this exact bundle at `--max-model-len 32768` successfully (peak KV 25.9%), so the raw
arm fits with margin. The compile arm's largest packet is ~2,930 tokens, far below the window.

## 9. Anti-tuning rule

**One iteration is allowed ONLY for tooling/environment failures** (a crash, a timeout, an OOM,
a vLLM launch/context-length rejection of a warm-up, or a wiring bug in `evals/a4_concurrency.py`
that prevents measurement). Such an iteration is disclosed exactly in the result doc. **Never**
after seeing results: the fixture, objective symbols, arms, tag format, `max_tokens`,
`temperature`, N levels, repeats, comparison rule, error classification, or max context length
are frozen as of this document. If compile fails a comparison, it is reported as failed; a new
experiment needs a new, separately dated pre-registration.

## 10. Dry-run token table (measured pre-run, offline)

Fixture: 10 files, 32,259 tiktoken; cartridge `cfx_httpx-b4_53833d8a`, 595 tokens,
`kv_prefix_hash sha256:78be32b2…`. Raw fit: 32,346 tokens, headroom 422 (fits).

| metric | raw | compile |
|---|---:|---:|
| prompt tokens, total (32 requests) | 1,032,955 | 93,106 |
| prompt tokens, per request (range) | 32,279 – 32,282 | 2,885 – 2,930 |
| prompt tokens, per request (mean) | 32,280 | 2,910 |
| compression (raw ÷ compile, per request) | — | **~11.1×** |
| shared prefix (cached once per burst) | 32,259 (bundle) | 595 (cartridge) |
| divergent per agent (≈ packet − cartridge − objective) | ~0 (only objective) | ~2,290 |

The dry-run receipt: `evals/reports/a4_dryrun_<ts>.json`. The raw arm's prompt-token reduction
is not its billing advantage here (the identical prefix is cached); the numbers that decide the
experiment are the **peak KV** and **queueing** at concurrency, which the live run measures.

## 11. Run environment (fixed now)

WSL2 Ubuntu, `~/.venvs/vllm`, `vllm serve Qwen/Qwen2.5-1.5B-Instruct --enable-prefix-caching
--max-model-len 32768 --enforce-eager --gpu-memory-utilization 0.85 --port 8000`. One vLLM
session per arm, restarted between arms; a tiny `temperature=0.0` engine warm-up request
precedes each session (the established recipe). GPU: RTX 5070 Laptop 8 GB (single GPU, sole GPU
worker). 5 N-levels × 3 repeats per arm; expected wall time is dominated by the raw arm's
repeated ~32k cold prefills.

## 12. Artifacts (to be produced)

- `evals/reports/a4_dryrun_<ts>.json` — the offline token table (§10).
- `evals/reports/a4_raw_<ts>.json`, `evals/reports/a4_compile_<ts>.json` — live receipts
  (per-cell metrics, error kinds, notes).
- `docs/engineering/A4-CONCURRENCY-RESULT.md` — method, per-N results table, the pre-registered
  verdict, caveats, artifact pointers.

Pre-stated caveats: single GPU; one 1.5B model; a synthetic 32-role workflow on real code;
objectives are unscored (the benchmark measures memory/concurrency, not accuracy); peak gauges
are ~25 ms samples (may miss faster transients); every repeat includes one cold prefill of the
shared prefix, so throughput/latency include the cold-start cost rather than a steady-state
warm-cache number.
