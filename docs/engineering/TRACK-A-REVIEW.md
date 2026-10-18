# Track A Adversarial Review — `cfx`, writable memory, relations

Scope: `sidecar/` (CLI, writable-memory endpoint, robustness batch), `kernel/store/`
(relations + closure integration), tests, and `SIDECAR.md` / `STORE.md`.
Method: full read of every changed file plus the router/protocol modules it touches;
adversarial socket-level probes (413, missing `Content-Length`, chunked, upstream death,
CORS on stdlib errors, payload spoofing, block-id charset, concurrency); a real `cfx
serve`/`python -m sidecar` subprocess against a fake upstream; real `cfx add`/`verify`
subprocesses; then the full gates.

## Verdict

**Pass with fixes.** No S1 (crash/corruption/security) defects found. Four S2 correctness
defects were found and fixed: three HTTP/1.1 framing/keep-alive faults (413/411 desync,
chunked request desync, non-SSE upstream-death hang) and the private-attribute connection
rebind, which was replaced with the requested additive `check_same_thread` constructor
option. Nine S3 robustness/validation gaps were also fixed. The concurrency race listed
in the brief is already closed by Track A's write lock (verified empirically; regression
tests added). 20 regression tests were added; 106 tests pass (baseline 86).

## Findings

Severity: S1 = crash/corruption/security, S2 = correctness, S3 = robustness/validation.
"Fixed" means this review changed code; "verified" means no defect was found in the
claimed behavior (tests added where valuable); "documented" means left as a named
residual risk.

| ID | Sev | Finding | Evidence | Fixed |
|----|-----|---------|----------|-------|
| A-01 | S2 | `413` responses (and invalid-`Content-Length` `400`) sent `Connection: close` but never set `close_connection`, so the unread body was parsed as the next request on the same socket (desync; probe showed a second connection abort instead of a clean close). | Probe: 413 + second request → `ConnectionAborted`; `sidecar/server.py:450` | yes |
| A-02 | S2 | Requests without `Content-Length`, with duplicate `Content-Length`, or with `Transfer-Encoding: chunked`, silently read length 0 (or the first length); the leftover bytes were parsed as pipelined requests (probe: chunked POST returned `200` followed by a stdlib `400` HTML page on the same socket). | Probe chunked output; `sidecar/server.py:230` | yes |
| A-03 | S2 | Non-SSE upstream death mid-body: `_relay` buffered status/headers but raised before `end_headers()`, and `_proxy` treated the stream as "sent", so the keep-alive loop waited for another request — client hang. | Probe: `httpx.ReadTimeout`; `sidecar/server.py:382` | yes |
| A-04 | S2 | `open_store()` closed and rebound `SqliteStore._conn` (private attribute) to hide sqlite3's thread affinity, skipping the constructor path. | `sidecar/server.py:118` (old) | yes |
| A-05 | S3 | Stdlib-generated error responses (`501` unsupported method, `400` bad request line, …) bypassed CORS. | Probe: `PUT /fabric/health` → `501` with no `Access-Control-Allow-Origin` | yes |
| A-06 | S3 | Empty `content` / `ref_uri` accepted; empty content and empty ref collapsed to the same auto id/hash, and a `trusted` empty block could enter a packet as a blank line. | Probe: `{"content": ""}` → `201`, `sha256:e3b0c44…` (hash of empty) | yes |
| A-07 | S3 | Client `block_id` was unvalidated: newlines, spaces, and unbounded lengths accepted (store/render/error-message injection surface). | Probe: `block_id:"a\nb"` and 500-char id → `201` | yes |
| A-08 | S3 | Non-finite `confidence` (`NaN`/`Infinity`, accepted by Python's JSON parser) passed `build_block`, then crashed JCS canonicalization inside `store.append`; `cfx add` leaked a traceback instead of its documented exit codes. | Code: `sidecar/blocks.py:101`; `protocol/canonical.py:51` | yes |
| A-09 | S3 | SSE abort marker was written unconditionally at the current byte position, so a death mid-line could concatenate it onto a partial `data:` line, handing the client a corrupt pre-abort line. | Code: `sidecar/server.py:428` (old) | yes |
| A-10 | S3 | `ingest_relations_jsonl` accepted JSON relation values that were not strings (or empty), violating the `Relation = tuple[str, str, str]` contract and producing int ids in `load_relations()`. | Code: `kernel/store/sqlite_store.py:56` (old) | yes |
| A-11 | S3 | `cfx add` on an unopenable store (non-SQLite file at `--store`) raised a traceback, not the documented exit-code surface. | Probe: `sqlite3.DatabaseError` traceback; `sidecar/cli.py:87` | yes |
| A-12 | S3 | `cfx serve --fabric missing.db` raised a raw `FileNotFoundError` traceback (hit during the E2E smoke). | Probe: traceback before server start | yes |
| A-13 | S3 | `CF_PRICE_INPUT_PER_MTOK=nan` produced `estimated_input_cost_usd: NaN` — invalid JSON from `/fabric/stats`. | Code: `sidecar/server.py:95` | yes |
| A-14 | S2 | **Claimed race:** two handler threads computing `event_seq = max_source_event_seq() + 1` and appending concurrently → duplicate seqs / double-insert. Track A's `write_lock` already wraps build+append+fabric; empirically 40 concurrent distinct posts produced 40 unique seqs (all `201`) and 20 concurrent identical posts produced exactly one `201` + 19 deduped `200`. | Probe output; `sidecar/server.py:194` | verified (tests added) |
| A-15 | S2 | **Claimed:** relations out-of-band payload conflict reachable via `append_relation` alone. It is not: `append_relation` always writes the deterministic canonical payload, so a re-append compares equal and dedupes. The `StoreConflict` path fires only for rows inserted out-of-band (test inserts a non-canonical payload) and is handled correctly. | `tests/test_store_relations.py:79`; code review | verified |
| A-16 | S2 | Client-supplied `content_hash` / `lifecycle_status` / `token_estimate` / `source_event_seq` / `memory_tier` / `fidelity` were already ignored — `build_block` recomputes all of them; `trusted` is the only promotion switch. Added a regression test. | Probe: spoofed values absent from response and stored row | verified (test added) |
| A-17 | S2 | `trusted: true` re-post of an existing draft returns `200` with the **stored** block's lifecycle (`draft`), so re-posting cannot promote; docs now state this explicitly. | Probe/code; `sidecar/server.py:338` | verified (test added) |
| A-18 | S3 | `cfx verify` delegation matches `protocol/epoch.py`'s parser (`argv[0]` = file, `--pubkey` scanned), including when flags precede the file. Real subprocess: valid chain exit 0 `OK: 1 epoch(s) verified`; wrong key exit 1. | E2E smoke | verified |
| A-19 | S3 | No socket read timeout: a stalled client ties up a handler thread indefinitely (`rfile.read(length)`, then `rfile.readline`). | Code review | documented |
| A-20 | S3 | The `httpx.Client` created in `make_server` is never closed on shutdown (process-lifetime leak; tests leak one client per server). | Code review | documented |
| A-21 | S3 | Relayed responses forward only `Content-Type` plus the `X-Fabric-*` extras; all other upstream headers (cache-control, rate-limit, …) are dropped. Pre-existing behavior. | Code review | documented |
| A-22 | S3 | `_objective()` raises `TypeError` on a malformed multimodal part (e.g. `{"text": 1}`), dropping the connection without a response and without incrementing `errors`. | Code review | documented |
| A-23 | S3 | The shared-connection design assumes a serialized sqlite3 build; `open_store` does not assert `sqlite3.threadsafety == 3` (true here: SQLite 3.50.4 / CPython 3.12.13; not guaranteed everywhere). | `uv run python -c "import sqlite3; …"` | documented |
| A-24 | S3 | Cross-process writers (running sidecar + `cfx add` on the same DB) remain uncoordinated; already named in `STORE.md` as the v0 single-writer limit. | Docs/code review | documented |
| A-25 | S3 | The 32 MB body cap bounds but does not prevent CPU abuse: `tiktoken` tokenizes the whole block synchronously per request. | Code review | documented |

Totals: 0 S1, 4 S2 fixed (+4 S2 claims verified safe), 9 S3 fixed, 1 S3 claim verified
(A-18), 7 S3 residual risks documented.

## What was changed

1. **Private-attr hack removed (A-04).** `SqliteStore(path, *, check_same_thread: bool =
   True)` is the new additive option (`kernel/store/sqlite_store.py:84`); the sidecar calls
   `SqliteStore(path, check_same_thread=False)` (`sidecar/server.py:118`). Default behavior
   for every existing caller is byte-identical (thread affinity preserved; a new store
   test asserts the default still raises `sqlite3.ProgrammingError` cross-thread and the
   opt-in works).
2. **Keep-alive/framing (A-01…A-03).** `_read_body` rejects missing `Content-Length`
   and chunked bodies with `411`, duplicate `Content-Length` with `400`; all `close=True`
   responses set `self.close_connection` (not just the header); `_proxy` tracks
   `_response_started`, `_relay` reads non-SSE bodies before starting the response so
   upstream death maps to a clean `502`, and a mid-relay failure closes instead of
   desyncing.
3. **CORS centralized (A-05).** `Handler.send_response` injects
   `Access-Control-Allow-Origin: *` and `Access-Control-Expose-Headers:
   X-Fabric-Compiled,X-Fabric-Packet-Id` on every response path, including `send_error`.
   `OPTIONS` still returns `204` with the allow-methods/headers/max-age set; a
   browser-style preflight for `POST /v1/chat/completions` with the `X-Fabric-*` headers
   succeeds.
4. **Payload validation (A-06…A-08).** Non-empty `content`/`ref_uri`; `block_id` 1–128
   characters with no whitespace/control characters; finite numeric `confidence`. Derived
   fields stay server-computed.
5. **SSE abort marker (A-09).** Newline-prefixed when the last read ended mid-line.
6. **Relations ingest (A-10).** `_relation_from_json` requires a JSON object with
   non-empty string ids/type; `StoreConflict` semantics and `rel_seq` ordering unchanged.
7. **CLI robustness (A-11, A-12).** `cfx add` returns 2 on an unopenable store (1 on
   store write errors); `cfx serve` returns 2 with a clean message for a missing fabric
   file (still never creates it implicitly). Happy paths, `--file -`, exit codes 0/1/2,
   and `--help` are unchanged and covered by tests.
8. **Stats JSON (A-13).** Non-finite price is ignored instead of emitting `NaN`.

## Threading model decision

**Chosen:** `ThreadingHTTPServer` with **one shared `sqlite3` connection** opened with
`check_same_thread=False`, plus a per-server `write_lock` around the whole
build→append→publish critical section (`sidecar/server.py:194`). Rationale:

- `source_event_seq = max_source_event_seq() + 1` and dedupe are read-modify-write
  sequences on one connection; serializing all writes makes them atomic. This is one lock
  in one function, and it is the smallest change that makes the endpoint correct.
- Reads (startup load, and `stored_block` which runs inside the lock) are safe to
  interleave: this interpreter's sqlite3 is serialized (`threadsafety == 3`, verified on
  CPython 3.12.13 / SQLite 3.50.4), so the module serializes access to the connection.
- Rejected alternatives: per-thread connections (store refactor plus WAL busy handling,
  unnecessary for a local single-operator tool); a writer thread/queue (more machinery
  for the same guarantee); locking inside `SqliteStore` (would silently change semantics
  for all existing callers instead of an opt-in flag). Upgrade path if concurrent
  cross-process writers ever appear: per-thread connections + `PRAGMA busy_timeout`.
- The model is documented in `SIDECAR.md` ("Threading model") and `STORE.md` ("v0
  limits"); the `check_same_thread` default keeps previous behavior for kernel callers.

## Commands run and observed results

| Command | Result |
|---------|--------|
| `uv run ruff check .` | `All checks passed!` (also fixed Track A's E501 in `tests/test_sidecar.py`) |
| `uv run ruff format --check .` | `89 files already formatted` (Track A's `STORE.md` Python fence and `sidecar/blocks.py` needed formatting) |
| `uv run mypy protocol evals kernel sidecar` | `Success: no issues found in 40 source files` |
| `uv run python -m pytest -q` | **106 passed** (baseline 86 + 20 new); `pytest.exe` avoided per Application Control policy |
| Adversarial probe (temp script) | Before: 413 desync/abort, chunked → `200`+`400` HTML on one socket, missing CL accepted, non-SSE death → `ReadTimeout`, `PUT` → `501` without CORS, empty content → `201`, newline id → `201`. After: `411`/`413` with `Connection: close` and clean close, `502` for upstream death, CORS on `501`, `400` for empty text and bad ids, spoofed fields ignored, 40/40 unique event seqs, 1×`201`+19×`200` for a raced duplicate |
| E2E subprocess smoke (`python -m sidecar` + fake upstream) | draft POST → `201 draft`, compiled prompt **excludes** it; trusted POST → `201 routable`, compiled prompt **includes** it; stats `{requests: 2, compiled: 2, errors: 0, upstream_prompt_tokens: 22}` |
| Real CLI subprocesses | `cfx add` exit 0 (newline-formatted receipt), dedupe exit 0 + `already present`, invalid payload exit 2; `cfx verify` real signed chain exit 0 `OK: 1 epoch(s) verified`, wrong key exit 1; `cfx --help` exit 0 |

Docs/code reconciliation: `SIDECAR.md` now documents request framing (411/413 + close),
CORS on all response paths including errors, dedupe-reported stored fields, server-computed
derived fields, block-id/text validation, the threading model, and CLI exit codes.
`STORE.md` now documents the out-of-band relation-conflict path, relations ingest
validation, and the `check_same_thread` option. `IMPLEMENTATION-STATUS.md` and
`CHANGELOG.md` were not touched.

## Residual risks (named, not hidden)

- **Local trust boundary.** No auth; `ACAO: *`; any local process (or prompt-injected
  tooling) can read the fabric and write `trusted` blocks. Documented in `SIDECAR.md`.
- **Slow clients.** No socket read timeout; a stalled upload pins a handler thread
  (A-19). Add `HTTPServer.timeout`/per-connection deadlines if the port is ever exposed
  beyond localhost.
- **CPU on large bodies.** Up to ~32 MB per block is tokenized synchronously (A-25);
  lower `CF_MAX_BODY_BYTES` for untrusted callers.
- **Cross-process writers.** Sidecar + `cfx add` on the same DB are uncoordinated
  (A-24); single-writer is the documented v0 contract.
- **Platform dependence.** The shared-connection model assumes `sqlite3.threadsafety ==
  3`; `open_store` does not assert it (A-23).
- **Relay fidelity.** Only `Content-Type` plus `X-Fabric-*` extras are relayed upstream
  headers; other upstream headers are dropped (A-21). The `httpx.Client` leaks until
  process exit (A-20). Malformed multimodal parts can drop a connection without a
  response (A-22).
- **Epoch/closure paths** were exercised only through existing tests and the DB-backed
  closure integration test; no defects found in `policy_engine` / `dependency_closer` /
  `compiler` interaction with stored relations.
