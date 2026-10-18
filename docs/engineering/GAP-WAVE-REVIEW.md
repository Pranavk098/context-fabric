# GAP-WAVE-REVIEW — adversarial audit of the four gap-closure changes (2026-10-17)

> **Historical snapshot (dated 2026-10-17).** All findings were fixed or documented at the
> time; the gate count (173 tests then; 226 today) and file counts in the command log are of
> that moment.

Scope: BM25/embeddings recall (`kernel/router/recall.py`, `kernel/router/embeddings.py`,
`kernel/router/compiler.py` seed, `tests/test_recall.py`), λ-MMR packing + U-curve recap
(`kernel/router/packer.py`, `sidecar/render.py`, `tests/test_packer.py`,
`tests/test_render.py`), MCP stdio server (`sidecar/mcp.py`, `sidecar/cli.py`,
`tests/test_mcp.py`), perf budget + hints (`tests/test_perf.py`, `kernel/router/hints.py`,
`tests/test_hints.py`). Adversarial review only; no other agents' work was reverted.

## Verdict

**Ship after the fixes below (all applied).** Two S1 defects (both in the MCP stdio loop)
and three S2 correctness defects were found and fixed with regression tests. Gates green:
`ruff check`, `ruff format --check`, `mypy`, and **173 passed** with
`uv run python -m pytest -q` (`pytest.exe` blocked; module invocation used). The packing
benchmark's control arm was measuring the wrong algorithm; the fix changed the control
numbers, so `docs/engineering/PACKING-QUALITY.md` was corrected (not a forbidden file).

## Findings

| ID | Sev | Area | Finding | Evidence | Fixed |
|----|-----|------|---------|----------|-------|
| GW-01 | S1 | MCP `receipt_verify` | An epoch whose string field contains a lone surrogate (`\ud800`) but whose Merkle root is valid reached `epoch_content_hash` and raised `UnicodeEncodeError` out of `handle()`, killing the stdio process (returncode 1, no reply). Reachable whenever `CF_RECEIPT_PUBKEY` is set (pre-check passes with the right `key_id`). | Repro: scripted `python -m sidecar.mcp` session; stderr traceback `canonical.py:107`; no response line, process exit 1 | yes — `sidecar/mcp.py:305-310` catches `(ValueError, UnicodeError)` around `verify_chain` → `-32602`; `tests/test_mcp.py::test_receipt_verify_lone_surrogate_is_rejected_not_fatal` |
| GW-02 | S1 | MCP stdio loop | A line with malformed UTF-8 bytes raised `UnicodeDecodeError` inside `for line in sys.stdin`, uncaught → server died instead of answering `-32700` and continuing (docs promise "malformed JSON line … keeps serving"). | Repro: `b'\xff\xfe{...}\n'` to stdin → traceback `mcp.py:343`, exit 1, empty stdout | yes — `_configure_stdio` now `reconfigure(encoding="utf-8", errors="replace")` (`sidecar/mcp.py:360-366`); `tests/test_mcp.py::test_malformed_utf8_line_recovers` |
| GW-03 | S2 | packer | `pack(..., mmr_lambda=1.0)` did **not** reproduce the pre-MMR greedy selection: tier 1 was ordered by utility/block_id instead of the legacy event-seq admission order, so the packing bench's "greedy" control measured a different algorithm (and the stored `PACKING-QUALITY.md` numbers were wrong). | Repro: d1 (seq 1, utility −0.2) vs d2 (seq 2, utility 1.8), tier1 fits one → legacy `['d1']`, `pack(lam=1.0)` `['d2']` | yes — `kernel/router/packer.py:167-172` bypasses `_mmr_order` for tier 1 at λ=1.0; `tests/test_packer.py::test_mmr_lambda_one_reproduces_legacy_greedy_tier1`; `PACKING-QUALITY.md` §2 corrected |
| GW-04 | S2 | render recap | Recap only skipped blocks whose **entire** content equalled a prohibition. The packer distills prohibitions to the first 12 words, so any negative-knowledge block longer than 12 words was rendered twice (truncated prohibition + 64-word recap). | Repro: 30-word NK block → prohibition line and full recap line both present | yes — `sidecar/render.py:51-52` also matches the 12-word prefix; `tests/test_render.py::test_recap_skips_long_block_whose_prohibition_prefix_is_rendered` |
| GW-05 | S2 | embeddings hybrid | `hybrid_scores` did `bm.pop("idf", None)`, silently dropping the legitimate block whose `block_id == "idf"` from hybrid results (compiler then seeds it with 0.0 even when lexically relevant). Contrived (block ids are usually `blk_*`) but reachable: `build_block` accepts any non-whitespace id. | Repro: blocks `idf`/`z`, query "canonical" → hybrid returned `{'z': ...}` only | yes — select bm25 scores by block id (`kernel/router/embeddings.py:84-88`); `tests/test_recall.py::test_hybrid_keeps_block_whose_id_is_the_idf_sentinel` |
| GW-06 | S3 | recall | `bm25_scores` uses the literal key `"idf"` for the corpus mean-IDF entry; a real block with that id overwrites it (the block's own score is still correct, so the compiler seed is unaffected, but consumers of the mean-IDF stat get the wrong value). Documented; naming fix would break the public shape. | `bm25_scores("canonical", [mk("idf", "unrelated")])["idf"] == 0.0` | no (documented) |
| GW-07 | S3 | packer quality | With the control fixed, the packing bench shows the default λ=0.7 selects 5/6 labeled must-keep decisions vs 6/6 for legacy admission (redundancy 0.178 vs 0.206, selected 28 vs 27). `answer-1` is dropped in favor of its surviving near-duplicate `dup-1-1` (same 7443 fact). This is the intended diversity trade-off, measured honestly; λ or per-kind policy is the knob if answer-key recall matters. | `evals/packing_bench.measure()` in-memory run; see corrected table in `PACKING-QUALITY.md` | no (design trade-off; doc corrected) |
| GW-08 | S3 | embeddings | The model **name** is pinned (`BAAI/bge-small-en-v1.5`) but no Hub **revision** is passed; fastembed 0.9.0 resolves the repo revision at download time (`snapshot_download` with no `revision`), so a cold cache can fetch a newer snapshot. ADR-0003 wants a revision pin; module docstring previously claimed a "pinned model download". | `uv.lock` fastembed 0.9.0; inspected `fastembed.common.model_management` | no (docstring corrected; already flagged in `RECALL-BENCH.md`) |
| GW-09 | S3 | recall/compiler | The new shared tokenizer drops 1-char tokens and splits underscores, so `lexical_relevance` and the compiler seed predicate are not identical to the pre-BM25 `[a-z0-9_]+` behavior for those edges (e.g. objective `"x"` seeded any block containing token `x` before; none now). Comment/docstring said "byte-compatible"/"preserving the old semantics"; corrected. Kernel tests do not cover 1-char/underscore objectives. | `lexical_relevance("x", ...) == 0.0`; `tokenize("Hello, WORLD_x 7 a") == ["hello","world"]` | no (docs corrected; behavior is the documented tokenizer contract) |
| GW-10 | S3 | MCP `memory_search` | Search reads the raw store: it filters `draft` only, so `validated`/`superseded`/`expired`/`redacted` blocks are returned by default, and no trust-domain/RBAC gate is applied (the stdio server has no identity to gate on). Documented tool contract is store search, but redacted content is a real leak if the store is shared. | Code read (`sidecar/mcp.py:256-281`); policy comparison in `kernel/router/policy_engine.py:36-38` | no (scope decision; flagged) |
| GW-11 | S3 | MCP `memory_add` | `trusted: true` lets any connected MCP client write routable (non-quarantined) memory into the fabric; the stdio server has no auth. Intended parameter, but it is a privilege escalation relative to `cfx add --trusted` (human-invoked). | `sidecar/mcp.py:312-328` | no (intended; flagged) |
| GW-12 | S3 | packer perf | The MMR saturation short-circuit uses the global minimum token estimate; one `token_estimate == 0` candidate makes `min_est == 0`, so the O(n²) similarity loop runs to completion after saturation. Correctness unaffected; only a pathological-cost edge. | Fuzz ran clean; code read `packer.py:98-109` | no (documented) |
| GW-13 | S3 | perf test | Docstring claims bounds are "roughly 5-7x the measured dev-box means"; measured 66.7 ms against the 250 ms bound is ~3.7x (render 0.01 ms vs 50 ms). The render line labelled "@ 1000-block packet" renders only the ~30 included blocks. No sleeps, mean-of-30, warm-up — not flaky today, but slower CI has less headroom than claimed. | `pytest tests/test_perf.py -q -s`: 5.20/14.38/66.71 ms; 2.36 s test | no (documented) |
| GW-14 | S3 | MCP errors | A message with `id` but non-string `method` is silently ignored (treated as a response to a server-initiated request), rather than `-32600`. JSON-RPC-ambiguous input; no crash. | Scripted session: `{"jsonrpc":"2.0","id":1,"method":null}` → no reply | no (documented) |

## Priority checks that passed (verified, not trusted)

- **BM25**: positive RSJ IDF `log(1+(n−df+0.5)/(df+0.5))`, `k1=1.2`/`b=0.75`, corpus stats computed over the passed candidate set, query terms summed in sorted order; fuzz over a unicode/tie corpus (30 blocks × 7 queries) found 0 violations of `score > 0 ⇔ query token present`; exact 0.0 otherwise; deterministic repeat runs. Invalid `CF_RECALL` raises `ValueError` (intended, tested); missing fastembed falls back to BM25 via `None` (`tests/test_recall.py:152-165`); typo'd modes fail loudly, which is the documented behavior, not silently masked.
- **Embeddings**: lazy import only on the semantic path; `_max_normalized` clamps all-zero/negative sets to 0.0 (no NaN/inf); `_cosine` guards zero norms; hybrid = `0.5·bm25_norm + 0.5·cos_norm`; extra is installed in this env (0.9.0) so the semantic tests actually ran (18/18 passed, no skips).
- **Compiler seed**: zero-overlap blocks are still excluded unless closure pulls them (`compile(objective="alpha", fact="unrelated…")` → only always-kinds + closure); determinism holds (dict/list orders are input-derived; `close()` sorts ids).
- **λ-MMR**: 20-repeat determinism (1 distinct outcome over a 40-block corpus); budget admission fuzzed over 50 random setups — no tier or hard-total overshoot; `Tier1Ledger` ascending-seq invariant holds after MMR selection; Jaccard is pure set arithmetic; tier-3 λ=1.0 path equals legacy utility order; probes stay admitted-first; prohibitions unchanged.
- **Render**: recap always last, ≤2 blocks, ≤64 words + ellipsis, `CF_RECAP=0` disables, works with `recency_anchor=None`; module docstring's tier-1 ordering claim matches the `Tier1Ledger` validator (`protocol/models.py:174-179`).
- **MCP**: int/str/null ids echoed; notifications (including unknown) never answered; `ping` → `{}`; stdout carries protocol only (all logging to stderr; scripted sessions grep-checked); version negotiation counter-offers 2025-11-25 on unknown; tool schemas valid-shaped; quarantine filter excludes drafts by default and includes them with `include_quarantined`; `memory_add` draft vs routable; `--readonly` → `-32001` and refuses to create a missing store; single-threaded line loop, no locks.
- **Hints/perf**: `prefix_hash` unchanged by tier-3 observations, changed by instruction/tier0; trace is not part of the packet (`packet_content_hash` computed before `trace["cache_hints"]`; `test_kernel` re-verifies); no sleep-based timing; `evict_priority` thresholds exactly 25 %/75 % with deterministic integer tests.

## Files changed by this review

| File | Change |
|------|--------|
| `kernel/router/packer.py` | λ=1.0 tier-1 legacy admission; docstring |
| `sidecar/render.py` | recap skips 12-word prohibition prefixes; docstring |
| `sidecar/mcp.py` | stdin `errors="replace"`; `receipt_verify` catches `UnicodeError`/`ValueError` |
| `kernel/router/embeddings.py` | hybrid selects bm25 scores by block id; revision-pin caveat in docstring |
| `kernel/router/recall.py`, `kernel/router/compiler.py` | corrected "byte-compatible"/"preserving old semantics" comments |
| `tests/test_packer.py`, `tests/test_render.py`, `tests/test_mcp.py`, `tests/test_recall.py` | 5 regression tests for GW-01…GW-05 |
| `docs/engineering/PACKING-QUALITY.md` | correction: benchmark control is the pre-MMR packer; new arm numbers and honest must-keep trade-off |

## Commands run

```powershell
uv run ruff check .                        # All checks passed
uv run ruff format --check .               # 113 files already formatted
uv run mypy protocol evals kernel sidecar  # Success: no issues found in 47 source files
uv run python -m pytest -q                 # 173 passed in ~51 s
uv run python -m kernel.router.recall      # self-check OK
uv run python -m kernel.router.embeddings  # self-check OK
```

Adversarial smokes (scripts, not committed): BM25 unicode/tie iff-fuzz; pack budget and
determinism fuzz (50 trials); 20-repeat MMR determinism; render long-NK duplication repro;
scripted MCP sessions for ids/notifications/params/UTF-8/surrogate-epoch (crash repro
before fix, `-32602` + `ping` after); in-memory `packing_bench.measure()` before/after the
control fix; `pytest tests/test_perf.py -s` timings.

## Residual risks

- **Model revision float** (GW-08): pin the fastembed snapshot (or vendor the model source
  with a revision) before relying on embeddings for replay-comparable receipts. Also note a
  cold cache means a network download on the first `CF_RECALL=embeddings` compile.
- **MMR must-keep trade-off** (GW-07): default λ=0.7 can drop a unique decision that
  resembles an already-selected block; the bench fixture shows 5/6 vs 6/6. Tune λ or add a
  must-keep/`decision` exemption if answer-key recall matters in production.
- **`memory_search` scope** (GW-10) and **`trusted` writes** (GW-11): the stdio server is a
  local, unauthenticated admin surface over the whole store. Do not expose it beyond the
  trust boundary of the fabric file.
- **Tokenizer edges** (GW-09): 1-char/underscore objectives now seed differently; no kernel
  test pins the edge cases. If seed behavior for such objectives matters, add a test before
  changing tokenizers again.
- **Perf headroom** (GW-12/13): 3.7x on the dev box; zero-token blocks defeat the MMR
  saturation shortcut. Re-measure before tightening the 250 ms bound.
- **`"idf"` sentinel** (GW-06): rename to a non-representable key (or return a wrapper) if
  the BM25 API is ever made public; compiler seeding itself is safe today.
