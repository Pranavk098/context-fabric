# Pilot Pre-Registration — raw-repo vs compiled-packet A/B on a real repository

**Status: PRE-REGISTERED. Frozen 2026-10-18 08:26 UTC, before any model output or run on
this fixture.** The author (builder/experiment agent) has not executed the pilot, the smoke
run, or any model request against this fixture at the time of writing. Everything below
(fixture, objectives, answer keys, arms, measurement, criteria) is fixed now; deviations
are allowed only under the anti-tuning rule at the bottom.

## 1. Question

On a real repository at real context size (raw prompt ≥ 30,000 tokens per request), can the
Context Fabric sidecar's compile mode (SQLite fabric + CPP v1.0 compiler, 4,000-token
budget) reduce prompt tokens by ≥3× versus pasting the repository while keeping answer
accuracy parity on a small model (Qwen2.5-1.5B-Instruct, local GPU)?

This is an engineering pilot on real bytes, not a benchmark claim. n = 6 objectives, one
model, one GPU, one run each of each arm (plus a smoke pass).

## 2. Fixture (fixed now)

- Repo: `https://github.com/encode/httpx`, `git clone --depth 1`.
- Exact commit: `b5addb64f0161ff6bfe94c124ef76f6a1fba5254` (2026-02-23, "Adapt
  test_response_decode_text_using_autodetect for chardet 6.0 (#3773)").
- Clone path used for the run:
  `C:\Users\prana\AppData\Local\Temp\opencode\httpx_pilot` (WSL:
  `/mnt/c/Users/prana/AppData/Local/Temp/opencode/httpx_pilot`).
- Bundle rule: take the `httpx/` package `.py` files in sorted relative-path order and take
  the **first 10** — this is the smallest sorted prefix whose tiktoken count is ≥ 30,000
  (measured: 31,913 tiktoken o200k; Qwen2.5 tokenizer 31,867, measured pre-run with
  `transformers`). Exact file list (in order):
  1. `httpx/__init__.py`
  2. `httpx/__version__.py`
  3. `httpx/_api.py`
  4. `httpx/_auth.py`
  5. `httpx/_client.py`
  6. `httpx/_config.py`
  7. `httpx/_content.py`
  8. `httpx/_decoders.py`
  9. `httpx/_exceptions.py`
  10. `httpx/_main.py`
- Rendering (deterministic, the only transformation): each file's raw UTF-8 bytes are
  concatenated in the order above, one blank line between files, each file prefixed by a
  header line `===== FILE: <relative path> =====`. No other edits. The exact rendered
  bundle is what the raw arm sends (as one system message) and what the token count in the
  fixture manifest refers to. A small variance note: the manifest counts the rendered
  header-inclusive text; expect ≈32,0xx tiktoken — the manifest value at run time is
  authoritative and is recorded verbatim in the artifacts.
- The fixture manifest (`evals/fixtures/pilot_fixture_<ts>.json`) records, per file: path,
  sha256 of the file bytes, byte size, tiktoken count; plus commit, bundle token totals, and
  the rendering rule — so a fresh clone at the same commit can audit the exact bytes.

## 3. Objectives and answer keys (fixed now; 6 roles, 1 objective each)

Keys are single case-insensitive required substrings, derived **by construction** from the
real source lines listed (raw bundle and fact blocks both carry them). The scorer is the
same function for both arms (case-insensitive substring; all keys required to pass).

| role | objective (sent as the user message) | key | source (real code) |
|---|---|---|---|
| architect | In the httpx repository, which module defines the `Client` and `AsyncClient` classes? Answer with the file path only. | `_client.py` | `httpx/_client.py:594` (`class Client`), `:1307` (`class AsyncClient`) |
| implementer | In httpx, which function do the top-level helpers `get()` and `post()` delegate to? Answer with the function name only. | `request` | `httpx/_api.py:195` and `:304` (`return request(`) |
| reviewer | In httpx, which exception is raised when a request exceeds the maximum number of redirects? Answer with the exception class name only. | `toomanyredirects` | `httpx/_exceptions.py:249` (`class TooManyRedirects`); raised at `httpx/_client.py:972` |
| tester | In httpx/_config.py, what is the default maximum number of connections given by `DEFAULT_LIMITS`? Answer with the number only. | `100` | `httpx/_config.py:247` (`DEFAULT_LIMITS = Limits(max_connections=100, max_keepalive_connections=20)`) |
| auditor | Which class in httpx/_decoders.py decodes gzip (`content-encoding: gzip`) response bodies? Answer with the class name only. | `gzipdecoder` | `httpx/_decoders.py:85` (`class GZipDecoder`), mapping at `:383` (`"gzip": GZipDecoder`) |
| ops | Which module in the httpx package implements the command-line interface (the `httpx` CLI)? Answer with the file path only. | `_main.py` | `httpx/_main.py:313` (`@click.command`); console entry point `httpx = "httpx:_main:main"` in `pyproject.toml` |

Build-time assertions (in `evals/pilot.py`): every key must be present (case-insensitive) in
the rendered raw bundle before any request is sent. A key that fails this check aborts the
run (that would be a fixture/content error, not a result).

Known limitations of keys, stated up front: they prove information *presence* or absence in
the answer, not answer quality; `100` is a bare number (a wrong answer containing "100"
would false-pass); `request` occurs in prose ("request") so a wrong-but-verbose answer could
false-pass. These asymmetries apply equally to both arms.

## 4. Fabric

1. **Cartridge (the real Tier-0 compiler).** `evals/pilot.py` runs the shipped compiler on
   the fixture: `kernel.cartridge.ast_extractor.extract_tree` on `<fixture>/httpx` (stdlib
   AST; tests ignored per its default), `pagerank_map` over the symbol-reference graph, and
   `serializer.build_cartridge` with `budget_tokens=600`, `tenant_id="default"`,
   `project_id="httpx-pilot"`, `commit_hash=<fixture commit>`. The resulting `.cfx`
   `Cartridge` (id, hologram markdown, token count, kv prefix hash) is written to an
   artifact JSON. **Limitation, declared before the run:** the shipped sidecar
   (`sidecar/server.py`, off-limits to this task) has no cartridge parameter on its compile
   path, so the cartridge is compiled, hashed and recorded but is **not** injected into
   compiled prompts. The compile arm therefore measures store-only compilation. This is
   preregistered, not a post-hoc observation.
2. **Fact blocks.** 11 hand-derived blocks grounded in the bundle's real content (module
   map, key classes/functions, real file paths, real defaults), written into a temp SQLite
   store via the normal `SqliteStore.append` path with `lifecycle_status=routable`,
   `trust_domain="default"`, `memory_tier=ledger`, `fidelity=extract`, `source_event_seq`
   1..11 ascending. Blocks (ids are stable):
   `blk_httpx_init`, `blk_httpx_version`, `blk_httpx_api`, `blk_httpx_auth`,
   `blk_httpx_client_classes`, `blk_httpx_client_redirects`, `blk_httpx_config`,
   `blk_httpx_content`, `blk_httpx_decoders`, `blk_httpx_exceptions`, `blk_httpx_main`.
   Each block's content is composed from the real file listed in its id's module and carries
   the answer key(s) for its objective (verified by the same construction check).
   The compile arm receives objectives only; the sidecar compiles the packet (budget_base
   fixed at 4,000 by the shipped server), renders it, and injects it as the system message.

## 5. Arms (fixed now)

- **raw**: sidecar in `passthrough` mode (no injection). Messages per request:
  `[{"role":"system","content": <rendered bundle>}, {"role":"user","content": <objective>}]`.
  Identical system message for all 6 objectives (the strongest naive baseline, matching the
  `cfx demo` fleet design); the real bytes, stable order. Agent/role identity is not encoded
  in raw prompts (nothing exists to carry it).
- **compile**: sidecar in `compile` mode over the 11-block store. Messages per request:
  `[{"role":"user","content": <objective>}]`; headers `X-Fabric-Agent: pilot-<role>`,
  `X-Fabric-Role: <role>` so the compiled instruction/recipient differ per role.
- Order: **raw first, then compile**, in one vLLM session. Cache warmth is measured, not
  controlled (see Measurement); a smoke pass precedes the full run and its requests are
  excluded from the full-run accounting but do warm the upstream cache — noted honestly in
  the receipt.
- Smoke pass: 1 objective (architect) × both arms, `max_tokens=8`, full-size raw bundle, to
  validate wiring; its artifact is saved separately. Full run: all 6 objectives × both arms,
  `max_tokens=48`, `temperature=0.0`, non-streaming.

## 6. Measurement (fixed now)

Per request: prompt tokens, completion tokens, latency (from the OpenAI-compatible
responses), and for the compile arm the `X-Fabric-Compiled` header. Per arm: request count,
prompt/completion token totals, wall time, mean latency, pass counts. Upstream: vLLM
Prometheus `/metrics` counters parsed tolerantly (prefix-cache hits/queries deltas per arm,
KV-cache usage peak via a ~50 ms sampler) — the same helpers the demo uses; null when
unavailable. Sidecar: final `/fabric/stats` snapshot per server (requests/compiled/errors).
Price envs (`CF_PRICE_INPUT_PER_MTOK`) are left unset; no cost figures.

## 7. Success criteria (fixed now)

- **(a) Token reduction:** total prompt tokens across the 6 compile requests ≤
  (total prompt tokens across the 6 raw requests) / 3 (i.e. compile sends ≥3× fewer).
- **(b) Accuracy parity:** compiled passes ≥ raw passes − 1 (out of 6).
- **(c) No compile-path errors:** zero failed HTTP requests in the run, sidecar
  `errors == 0`, `compiled == 6`, and every compile response carries `X-Fabric-Compiled: 1`.

Verdicts are reported plainly as pass/fail, including partial or null results. No
cherry-picking: all six objectives are reported regardless of outcome.

## 8. Anti-tuning rule

**One iteration is allowed ONLY for tooling/environment failures** — a crash, a timeout, an
OOM, a vLLM launch/context-length rejection, or a wiring bug in `evals/pilot.py` that
prevents measurement. Such an iteration is disclosed exactly (what changed and why) in
`PILOT.md`. **Never** after seeing results: objectives, answer keys, fact-block content,
prompts, bundle file list, or success criteria are frozen as of this document. If the pilot
fails a criterion, it is reported as failed; a new pilot would need a new, separately dated
pre-registration.

## 9. Run environment (fixed now)

WSL2 Ubuntu, `~/.venvs/vllm`, `vllm serve Qwen/Qwen2.5-1.5B-Instruct --enable-prefix-caching
--max-model-len 32768 --enforce-eager --gpu-memory-utilization 0.85` (the established recipe
with a larger context window; the model's native window is 32,768). Single session: start →
warmup request → smoke → full run → kill leftovers. GPU: RTX 5070 Laptop 8 GB (single GPU).

## 10. Artifacts (to be produced)

- `evals/reports/pilot_<ts>.json` — full receipt (records, arms, cache, cartridge, stats,
  criteria verdicts, notes).
- `evals/reports/pilot_smoke_<ts>.json` — smoke receipt.
- `evals/reports/pilot_cartridge_<ts>.json` — the compiled cartridge.
- `evals/fixtures/pilot_fixture_<ts>.json` — fixture manifest (bytes/sha256/tokens).
- `docs/engineering/PILOT.md` — method, per-arm table, verdicts, caveats, artifact pointers.

Pre-stated caveats: single GPU; one 1.5B model; a synthetic 6-role workflow ON real code;
n = 6 objectives with substring keys; cache warmth from the smoke pass and from the raw
arm's shared system message; the cartridge is compiled but not injected (sidecar limitation,
declared above); a run per arm, no repeats.
