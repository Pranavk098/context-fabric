# Sidecar ("the plug") — Adversarial Review

**Status:** Complete. Reviewed 2026-10-17 against the working tree containing `sidecar/`,
`tests/test_sidecar.py`, `docs/engineering/SIDECAR.md` (all untracked; no commit made).
**Reviewer:** auditor agent. **Method:** code reading plus live adversarial runs: in-process
smoke harness (fake upstream), raw-socket framing/header attacks, a real `python -m sidecar`
subprocess in both modes, and the full CI gate set.
**Scope:** `sidecar/server.py`, `sidecar/render.py`, `sidecar/__main__.py`, `tests/test_sidecar.py`,
`docs/engineering/SIDECAR.md`, plus the kernel policy path (`kernel/router/policy_engine.py`,
`kernel/router/compiler.py`) and protocol models it depends on.

## Verdict

No S1 (crash/corruption/security) defects found. Five S2 defects were found and fixed in place
(four in `sidecar/server.py`, one in `sidecar/__main__.py`) with regression tests. The plug is
**shippable for its stated local, single-operator use** after these fixes; residual risks are
documented below and none block shipping. All house gates pass on the reviewed tree.

## Findings

| ID | Sev | What | Evidence | Fixed |
| :--- | :--- | :--- | :--- | :--- |
| **SR-01** | S2 | Non-numeric or negative `Content-Length` crashed the request handler: `int(...)` raised `ValueError`, socketserver printed a traceback and closed the connection with **no HTTP response**. | Raw socket `Content-Length: notanumber` → client received `b''`, stderr traceback at `server.py:114`. | **Yes** — invalid/negative → `400 {"error": ...}`; regression test `test_invalid_content_length_returns_400`. |
| **SR-02** | S2 | gzip-compressed SSE was relayed as **raw compressed bytes** with no `Content-Encoding` header: the client received undecodable garbage labeled `text/event-stream`. Caused by `iter_raw()` while httpx itself advertises `Accept-Encoding: gzip, deflate` upstream. | Fake upstream returning gzip `text/event-stream`: passthrough delivered `\x1f\x8b\x08...` (pre-fix smoke 25/28). Non-streaming path was already safe (`resp.read()` decodes). | **Yes** — `iter_bytes()` (decodes incrementally); regression test `test_gzip_sse_upstream_is_decoded`. |
| **SR-03** | S2 | Headers named in the client's `Connection:` header (RFC 7230 §6.1 hop-by-hop) were forwarded upstream, along with the other standard hop-by-hop headers (`keep-alive`, `te`, `trailer`, `upgrade`, `proxy-*`). | `Connection: close, X-Custom-Hop` + `X-Custom-Hop: smuggled` → upstream saw `X-Custom-Hop: smuggled`. | **Yes** — static hop-by-hop set extended and `Connection`-listed tokens stripped per request; regression test `test_connection_listed_hop_headers_are_stripped`. |
| **SR-04** | S2 | `--upstream` containing a non-printable character raised `httpx.InvalidURL`, which is **not** an `httpx.HTTPError`: unhandled → traceback, no response (client `RemoteProtocolError`). | `make_server(upstream="http://bad\x01host")` → stderr traceback at `server.py:175`; client got "Server disconnected without sending a response". | **Yes** — catch `httpx.InvalidURL` alongside `HTTPError` → `502`; regression test `test_bad_upstream_url_returns_502`. |
| **SR-05** | S2 | `CF_MODE=bogus` silently started the sidecar in a passthrough-equivalent state instead of failing: argparse does not validate env-provided defaults against `choices`. An "A/B compile arm" could silently be the control arm. | `CF_MODE=bogus .venv\Scripts\python.exe -m sidecar --port 8097` stayed up after 3 s. | **Yes** — post-parse `parser.error(...)`; regression test `test_invalid_cf_mode_env_is_rejected`. |
| **SR-06** | S3 | Upstream response headers other than `Content-Type` (e.g. `x-request-id`, rate-limit, `openai-processing-ms`) are dropped. | `_relay` copies only `Content-Type` + the two `X-Fabric-*` extras. | No — acceptable for chat clients; note only. |
| **SR-07** | S3 | No cap on request or non-streaming response body size: `rfile.read(length)` / `resp.read()` are unbounded (memory grows with client/upstream input). | Code reading, `server.py:134,216`. | No — trusted local client; documented residual. |
| **SR-08** | S3 | Chunked request bodies are not supported (stdlib `http.server` ignores `Transfer-Encoding`): the sidecar reads 0 body bytes and answers `400 request body must be JSON` in compile mode. | Raw chunked request → clean `400`. | No — stdlib limitation; all mainstream OpenAI SDKs/curl send `Content-Length`. Documented. |
| **SR-09** | S3 | `protocol_version = "HTTP/1.0"` (default): no keep-alive, each client request opens a new TCP connection; SSE responses are close-delimited. | `Server: BaseHTTP/0.6 Python/3.12` in responses; observed 200s. | No — correctness fine; per-request connection overhead only. |
| **SR-10** | S3 | For a **close-delimited** SSE stream, upstream death mid-stream is indistinguishable from a normal end: the client sees a clean EOF without `[DONE]` and `errors` stays 0. With vLLM-style **chunked** SSE, death is detected and counted (`errors: 1`) and the client still gets a clean close. | Dying-upstream tests: close-delimited → `errors: 0`; chunked mid-chunk close → `errors: 1`, exit verified. | No — cannot detect without SSE-aware parsing; client should key off `[DONE]`. Documented below. |
| **SR-11** | S3 | `render.py` omits `recency_anchor` (generic output-contract boilerplate). `negative_knowledge` prohibitions still appear because those blocks render through tier1. | Synthetic overflow packet: renders instruction, tier1, `[swap]` handles; anchor string absent. | No — anchor is generic; rendering it is optional polish. |
| **SR-12** | S3 | Duplicate request headers (e.g. repeated `Cookie`/`Accept`) are collapsed by the dict comprehension. | `server.py:193`. | No — irrelevant in practice for chat APIs. |
| **SR-13** | S3 | SIDECAR.md "Every request then carries the same bounded, prefix-stable project truth" is loose: the rendered instruction includes the per-request `OBJECTIVE` line; stability applies to the fabric blocks, not the whole system message. | `kernel/router/compiler.py:59` + render output. | No — doc nuance only. |
| **SR-14** | S3 | Missing/unreadable `--fabric` file and out-of-range `--port` fail at startup as raw tracebacks. | Code reading (`load_blocks`, `make_server`). | No — operator error path, message is self-explanatory. |
| **SR-15** | S3 | `errors` counter counts only upstream transport failures — not upstream 4xx/5xx, not client 400s. | `stats.add("errors")` only in `_proxy`'s except block. | No — docs describe it as "errors"; mild ambiguity. |

## Claims verification

| Claim / checklist item | Result |
| :--- | :--- |
| All SIDECAR.md flags/env vars exist as documented (`--upstream`/`CF_UPSTREAM`, `--port`/`CF_PORT`, `--fabric`/`CF_FABRIC`, `--mode`/`CF_MODE`; defaults upstream `http://127.0.0.1:8000`, port `8080`, mode `compile`) | **Verified** via `uv run python -m sidecar --help` and a subprocess run configured entirely from `CF_*` env vars. |
| Policy gate documented: `trust_domain == "default"`, `lifecycle_status == routable`, `allowed_roles`/`denied_roles` | **Verified in kernel** (`policy_engine.py:33-43`; compiler passes `trust_domain=scope.tenant_id="default"`, `as_of=DEFAULT_AS_OF`/now) and **end-to-end through the real CLI**: role `user` got only the unrestricted decision; a foreign-trust-domain decision and an expired decision were never rendered; a `allowed_roles=["builder"]` decision appeared only for `X-Fabric-Role: builder`. Matches SIDECAR.md troubleshooting text. |
| Builder claim "no reusable renderer existed in evals/harness.py" | **Verified** — `evals/harness.py` is an OpenAI-compatible client/metrics harness (token counting, prompt hashing, cache scraping), no packet→text renderer. `sidecar/render.py` is genuinely new. |
| `render.py` renders instruction + tier1/tier3 contents + tier2 summaries | **Verified** — instruction (`OBJECTIVE`/`STAGE`/`ROLE`), tier0 markdown, tier1 `[kind] content-or-ref`, tier3 shadow probes/observations, tier2 `[swap] summary (uri)`. Caveat SR-11. |
| `trust_env=False`; no other env leak | **Verified** — only `sidecar/__main__.py` reads `CF_*`; client is constructed with `trust_env=False`. |
| Forwarded request framing correct after injection | **Verified** — fake upstream read exactly the declared `Content-Length` and parsed the full JSON; `Host`/`Connection`/`Content-Length` regenerated by httpx; client's `Accept-Encoding` dropped (upstream saw httpx's own). |
| No `messages` / empty `messages` / non-list `messages` / non-dict JSON body / user content array / user `content: null` | **Verified** — untouched passthrough without `X-Fabric-Compiled`; content arrays compile with the concatenated text parts. |
| Streaming: no first-token buffering | **Verified** — upstream emitted the first SSE chunk immediately then slept 1.5 s; client saw it after **~20 ms**. |
| Errors: upstream down / invalid URL / malformed JSON / upstream 5xx | **Verified after fixes** — `502` JSON, `502` JSON, `400` JSON, upstream `500` relayed verbatim with body. |
| Concurrency | **Verified** — 12 parallel requests all `200`; `Stats` lock and shared `httpx.Client` (thread-safe) fine; `ThreadingHTTPServer.daemon_threads = True` so streams can't hang shutdown. |
| Header injection via CRLF | **Verified** — a bare-LF header line was handled without crash and without forwarding garbage; a real CR/LF cannot survive HTTP header parsing. |

## Executed (commands and observed results)

| Command | Observed |
| :--- | :--- |
| `uv run python -m sidecar --help` | Exit 0; four flags exactly as documented. |
| In-process smoke harness (temp script: fake upstream + sidecar, 28 checks incl. compile injection, passthrough-untouched, malformed JSON, upstream down/5xx, first-token timing, gzip SSE, raw-socket bad/negative CL, chunked request, bare-LF header, 12-way concurrency, `/v1/models`) | Pre-fix **25/28** (3 real defects, one was a harness assertion error); post-fix **28/28**. |
| Raw-socket hop-by-hop test (`Connection: close, X-Custom-Hop`) | Pre-fix: upstream received `X-Custom-Hop: smuggled`; post-fix: absent. Upstream `Connection` is always httpx's own `keep-alive` (client's value correctly not forwarded). |
| `http://bad\x01host` upstream via `make_server` | Pre-fix: stderr traceback, client `RemoteProtocolError`; post-fix: `502` JSON. |
| Dying-upstream tests (close-delimited and chunked SSE) | Close-delimited: first chunk delivered, clean EOF, `errors: 0` (SR-10). Chunked mid-chunk abort: first chunk delivered, clean EOF, `errors: 1`. |
| Real CLI run, env-configured (`CF_FABRIC`/`CF_UPSTREAM`/`CF_PORT`/`CF_MODE`), compile mode, 4-block fabric (restricted, foreign-domain, expired, plain) | `X-Fabric-Compiled: 1`; role `user` saw only the plain decision; role `builder` additionally saw the `allowed_roles=["builder"]` decision; foreign-domain and expired never appeared; `/fabric/stats` = `{"requests":2,"compiled":2,"errors":0,"upstream_prompt_tokens":10}`. |
| Real CLI run, `--mode passthrough` | Body untouched, no `X-Fabric-Compiled`. |
| `CF_MODE=bogus` subprocess | Pre-fix: stayed running (silent fallback); post-fix: `SystemExit(2)` with a clear message. |
| `uv run ruff check .` | All checks passed. |
| `uv run ruff format --check .` | 72 files already formatted. |
| `uv run mypy protocol evals kernel sidecar` | Success, 34 files. |
| `uv run pytest -q` | 33 passed (9 in `tests/test_sidecar.py`). |
| `uv run pytest tests/test_sidecar.py -q` | 9 passed. |

Note: while this review ran, another process/agent was concurrently writing
`protocol/epoch.py` / `tests/test_epoch.py` (they appeared mid-review, caused one transient
pytest collection error at a half-written line, and later settled). Final gate numbers above
are from the settled tree; those files are unrelated to and untouched by this review.

## Residual risks (none block shipping)

1. **Trust boundary is local and cooperative.** `X-Fabric-Agent`/`X-Fabric-Role` are
   unauthenticated client headers selecting RBAC policy. Any local process (or prompt-injected
   tooling) can claim a different role. Acceptable for a local single-operator plug; document
   it in any multi-user deployment.
2. **No body-size caps** (SR-07). A trusted client can push arbitrarily large prompts/upstream
   responses through memory. Local-only listener on `127.0.0.1` mitigates.
3. **Stream termination semantics** (SR-10). Clients must key off `[DONE]` rather than clean
   TCP close to detect a dead upstream on close-delimited SSE (vLLM streams are chunked, where
   the sidecar does count the error).
4. **Only `/v1/chat/completions` and `/v1/models` are proxied**; other OpenAI endpoints 404.
   Documented scope ("chat completion").
5. **HTTP/1.0 response framing** (SR-09): correctness-safe, but adds a TCP handshake per turn
   and cannot do HTTP/1.1 keep-alive; fine for chat workloads.
6. **Not tested here:** real vLLM (no GPU in this environment), HTTPS upstreams, and a client
   sending `"stream": true` against an upstream that answers non-SSE JSON (code path identical
   to plain JSON relay; nothing observed suggests an issue).
