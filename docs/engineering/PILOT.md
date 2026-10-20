# PILOT — raw repository vs compiled packet on real httpx bytes

Run date: 2026-10-18 (file timestamps `20261018_0131xx` are local time, UTC−7; the receipts'
`started_at` is 08:31 UTC), single WSL session on the local GPU.
Pre-registration: `docs/engineering/PILOT-PREREGISTRATION.md` (frozen 2026-10-18 08:26 UTC
before any run; no amendments were needed).

**Verdict: all three pre-registered criteria PASS.** In one line: the raw arm (a
32,259-tiktoken bundle of the real repository pasted as one system message) was compared
against the compile arm (the shipped sidecar compiling packets from an 11-block fact fabric)
on six role objectives, one model, one GPU. Compiled packets carried **35.9x** fewer prompt
tokens with **symmetric accuracy** (5/6 each arm; each arm missed a different objective).

## Fixture

- `https://github.com/encode/httpx` @ `b5addb64f0161ff6bfe94c124ef76f6a1fba5254`
  (2026-02-23), cloned `--depth 1`; clone path
  `C:\Users\<user>\AppData\Local\Temp\opencode\httpx_pilot`.
- Bundle: first 10 files of `httpx/**/*.py` in sorted relative-path order
  (`__init__.py`, `__version__.py`, `_api.py`, `_auth.py`, `_client.py`, `_config.py`,
  `_content.py`, `_decoders.py`, `_exceptions.py`, `_main.py`), raw bytes, `FILE:` headers,
  stable order. Rendered bundle: **32,259 tiktoken** (`o200k_base`); per-request vLLM prompt
  for the raw arm: 32,226-32,229 tokens. Per-file sha256 + byte/token counts in the fixture
  manifest, so a fresh clone at the same commit can audit the exact bytes.
- Six objectives with 1-2 required-substring keys each, all derived by construction from
  real source lines (table in the preregistration, section 3); a build-time construction
  check asserts every key exists in both the raw bundle and the fact fabric before any
  request is sent.

## Fabric

- **Cartridge (real compiler run):** `extract_tree` (stdlib AST) + PageRank + serializer
  over the fixture's `httpx/` package → `cfx_httpx-pilot_b5addb64`, 542-token hologram,
  `kv_prefix_hash` recorded in `pilot_cartridge_*.json`.
  *Declared limitation (preregistered):* the shipped sidecar's compile path takes no
  cartridge argument, so the cartridge is compiled, hashed and recorded but not injected;
  the compile arm measures store-only compilation.
- **Fact blocks:** 11 hand-derived blocks grounded in the bundle's real content (module
  map, key classes/functions, real paths, real defaults), appended through the normal
  `SqliteStore.append` path: `lifecycle_status=routable`, `trust_domain="default"`,
  `memory_tier=ledger`, `source_event_seq` 1..11 ascending. Every objective's key was
  verified present in the compiled packet before the GPU run (local dry run).

## Method

- **raw** (ran first): sidecar in passthrough mode; each of the 6 requests sends the same
  full bundle as a system message + the objective as the user message (strongest naive
  baseline; prefix-cache friendly by design).
- **compile**: sidecar in compile mode over the store; objective only as the user message,
  `X-Fabric-Agent`/`X-Fabric-Role` per role; the server compiles (budget_base=4000 fixed by
  the shipped server), renders, and injects the packet as the system message.
- Same scorer both arms: case-insensitive substring check, all keys required (demo.py's
  `score_answer`). Temperature 0.0, non-streaming, `max_tokens=48`; one repeat per arm.
- Engine: `vllm serve Qwen/Qwen2.5-1.5B-Instruct --enable-prefix-caching --max-model-len
  32768 --enforce-eager --gpu-memory-utilization 0.85` (RTX 5070 Laptop 8 GB). Smoke pass
  (1 objective, `max_tokens=8`) ran first in the same vLLM session; its requests are
  excluded from full-run accounting but warm the upstream cache (see caveats). Price envs
  unset.

## Per-arm results (full run, `pilot_20261018_013151.json`)

| arm | reqs | prompt tok total | mean prompt/req | completion tok | wall | mean latency | accuracy | cache hits+ / queries+ | KV peak |
|---|---|---|---|---|---|---|---|---|---|
| raw | 6 | 193,366 | 32,228 | 28 | 1.19 s | 193.8 ms | 5/6 | 193,184 / 193,366 (99.9%) | 0.259 |
| compile | 6 | 5,391 | 899 | 27 | 0.93 s | 154.2 ms | 5/6 | 896 / 5,391 (16.6%) | 0.007 |

Wall and latency are warm-cache numbers (see caveats); the prompt-token totals are the
load-bearing measurement. Other evidence in the receipt: sidecar stats `raw: requests 6,
compiled 0, errors 0`; `compile: requests 6, compiled 6, errors 0, upstream_prompt_tokens
5,391`; all 6 compile responses carried `X-Fabric-Compiled: 1`.

## Criteria verdicts (preregistered, applied as written)

| criterion | verdict | numbers |
|---|---|---|
| (a) compile sends >=3x fewer prompt tokens | **PASS** | 5,391 vs 193,366 → 35.87x fewer |
| (b) accuracy parity: compile >= raw − 1 (of 6) | **PASS** | compile 5/6, raw 5/6 (bound: >=4) |
| (c) no compile-path errors | **PASS** | 0 error rows, sidecar errors 0, compiled 6/6, X-Fabric-Compiled 6/6 |

Response-level detail (all six reported, pass or fail):

- raw missed **ops**: answered `httpx/cli/main.py` (a hallucinated path) instead of
  `_main.py` — failed to extract the fact from 32k tokens of real code.
- compile missed **tester**: answered `20` instead of `100`. The fact block carries both
  `DEFAULT_LIMITS = Limits(max_connections=100, ...)` and `DEFAULT_MAX_REDIRECTS = 20`; the
  1.5B model grabbed the wrong number. This is a model-attention miss on a correct packet,
  not a fabric or scorer defect.

Neither arm failed the same objective; the misses were symmetric, which is what the parity
criterion (b) was written to detect.

## Smoke pass

`pilot_smoke_20261018_013144.json`: 1 objective (architect), `max_tokens=8`. raw 1/1
(32,227 prompt tokens), compile 1/1 (897 prompt tokens), 0 errors, compiled 1/1. Wiring
(store → compile → render → inject → score → metrics) validated end to end before the full
run; no changes were made between smoke and full runs.

## Tooling-iteration exception

**None used.** Smoke and full runs both passed on the first GPU attempt; no crash, timeout,
OOM or wiring fix occurred. The frozen objectives, keys, fact-block content, prompts and
success criteria were not touched after preregistration.

## Caveats (honest, preregistered plus observed)

- Single GPU, single 1.5B model, n = 6 objectives, one repeat per arm; substring keys prove
  information presence/absence, not answer quality.
- Cache warmth: the smoke pass and the raw arm's shared system message leave the upstream
  prefix cache warm — the raw arm shows a 99.9% cache-hit ratio and ~0.19 s mean latency,
  which measures cached prefill, not cold-request cost. The token totals are unaffected.
  Prefix-cache counters are upstream-global (vLLM) and parsed tolerantly; the raw KV peak
  (0.259) was sampled at the start of the raw arm while the smoke run's 32k-token blocks
  were still resident — the compile arm's 0.007 is the uncontaminated reading.
- The cartridge is compiled for real and recorded, but the shipped sidecar cannot inject a
  Tier-0 cartridge (preregistered limitation); compiled prompts are store-only.
- The compile tester miss shows the fabric was correct but the small model misread one
  number among two in the same block; a larger model or split blocks could differ.
- `code` keys like `100` are substring checks (a wrong answer containing "100" would
  false-pass); this asymmetry applies to both arms.

## Artifacts

- `evals/reports/pilot_20261018_013151.json` — full receipt (records, arms, cache, sidecar
  stats, cartridge, criteria verdicts, notes).
- `evals/reports/pilot_smoke_20261018_013144.json` — smoke receipt.
- `evals/reports/pilot_cartridge_20261018_013151.json` (full run) and
  `evals/reports/pilot_cartridge_20261018_013144.json` (smoke) — compiled `.cfx` cartridge.
- `evals/fixtures/pilot_fixture_20261018_013151.json` (full run) and
  `evals/fixtures/pilot_fixture_20261018_013144.json` (smoke) — fixture manifest (paths,
  sha256, bytes, tiktoken per file; commit; rendering rule).
- Runner: `evals/pilot.py` (`python -m evals.pilot --fixture <clone> [--smoke]`).
