# SCRUM Review — uncommitted wave (adapters, repo fixture, fold/fork, shadow cortex, negscore v3, sidecar bench)

**Reviewer:** adversarial audit · **Date:** 2026-10-18 · **Tree:** uncommitted wave on HEAD `9c7d9ac`
**Method:** static read of every wave file + diff-heavy review (`git diff HEAD`, `git show HEAD:...`),
adversarial smokes of my own (temp repos, denied-role forks, edge packets, path-swapped benchmark
runs), then all repo gates. No commits made.

## Verdict per area

| # | Area | Verdict | Notes |
|---|------|---------|-------|
| 1 | `adapters/raw_llm.py` | **PASS** | `"\n".join(...) == render_packet()` holds in all smokes (empty packet, tier0-only, empty instruction, no cartridge + TTL, anchor-only, trailing newlines). `cache_control` sits on block 1 only; block 2 unpinned. TTL validated (`5m`/`1h`/None); ignored only when there is no cartridge, as documented. Deterministic (no clock/RNG). |
| 2 | `sidecar/repofixture.py` + `cfx demo --repo` | **PASS after fixes** | 2 real bugs fixed (R-01, R-02). Deterministic across runs (byte-identical fixture); 0-symbol repos raise a clear `ValueError` → CLI exit 2; `--repo` + `--scenario negative` rejected with exit 2; receipt schema changes are additive (`fixture`, `mock`, `cost`). Answer keys verified to appear in the compiled render for each objective. |
| 3 | `sidecar/foldfork.py` + `cow_forker` Tier-1 filter | **PASS with residual risk** | `total_tokens` math recomputed and verified numerically; `Tier1Ledger` ascending-`event_seq` invariant survives filtering; ledger object is shared by identity when nothing is denied; CLI fork applies the identical filter (verified end-to-end). Fold is idempotent per source content; changed `content_hash` mints a new fact (append-only) — doc clarified (R-04). Residual tier-2 summary leak documented (R-05). |
| 4 | `kernel/router/shadow_cortex.py` | **PASS** | No clock/RNG; probe ids unique per source (`blk_probe_<source_id>`); input-order independent total ordering. Smokes: empty probes/empty response, unicode (incl. CJK/emoji/curly apostrophes), `?`/`!` in fragment, 100k-char input, tie cases — no crash, deterministic verdicts. Contradiction cues precede acknowledgment cues in `_CUE_RE`, so an exact nearest-cue tie resolves to contradicted deterministically. |
| 5 | `sidecar/negscore.py` consolidation + shim | **PASS (one caveat)** | v1 and v2 verified byte-equivalent to HEAD `sidecar/demo.py`: constants sets/regexes identical, and 2,001 randomized cases produce identical results through the HEAD-sourced functions vs the new module. Shim re-exports the exact same objects (`is` identity) and `evals/negscore_compare.py` imports `sidecar.negscore` directly. `--freeze-traps`: unknown ids rejected (rc 2, before any request) and the pilot is provably skipped in `run_negative` when `freeze_traps is not None`. Caveat R-06: the pre-consolidation `evals/negscore.py` was never committed, so v3 could not be byte-diffed against a git baseline; verified against `docs/engineering/NEGATIVE-SCORER.md` + the v3 test suite instead. |
| 6 | `sidecar/server.py` `_Server` + repeat warning | **PASS** | Exactly 4 hunks vs HEAD, all wave-owned: (a) `_log_compile` docstring, (b) repeat-`packet_id` warning, (c) `_Server` class with only `request_queue_size = 256`, (d) `make_server` returning `_Server`. No other behavior changed. The warning's `store.get_compile` → `store.log_compile` pair runs entirely under the non-reentrant `write_lock`, with no nested acquisition anywhere in `_log_compile`/`log_compile` → cannot deadlock; covered by `test_repeat_packet_id_warns_and_first_receipt_wins`. |
| 7 | `evals/bench_sidecar.py` | **PASS** | Smoke ran 3× (no flakes) plus a reduced full-matrix run; stderr was empty, proving the per-cell objective suffix (`{mode} bench cell {cell} request ...`) prevents the manifest dedupe path from firing. `_percentile` verified as correct nearest-rank on hand-computed lists; `p50 ≤ p95 ≤ p99`; wall/throughput math consistent. Barrier release is deterministic; `BrokenBarrierError` would fail loudly, not flake silently. |
| 8 | Misc (costs, tests, CI/Makefile/CONTRIBUTING) | **PASS** | `sidecar/costs.py` arithmetic exact (unit-tested); `adapters` added to mypy/wheel/CI/Makefile docs consistently; `scripts/check_docs_numbers.py` green in CI and locally. |

## Findings

| ID | Sev | Area | Evidence | Disposition |
|----|-----|------|----------|-------------|
| R-01 | **S2 (crash)** | repofixture | `_select` called `path.read_bytes()` unguarded; a mocked `PermissionError` on a candidate reproduced `build_repo_fixture` crashing with a traceback (also possible via a file deleted mid-scan). Violates "unreadable files must skip gracefully". | **Fixed** — `sidecar/repofixture.py:134` now skips `OSError`; regression test `tests/test_repofixture.py:169`. |
| R-02 | **S2 (security)** | repofixture | `Path.rglob` + `is_file()` follows file symlinks; a `.py` symlink inside `repo_path` pointing outside would be read and embedded in the raw bundle (Linux; symlink creation is unprivileged there). | **Fixed** — `sidecar/repofixture.py:102,111` resolve each candidate and skip unless contained in the resolved repo root; test `tests/test_repofixture.py:186` (skips on Windows without symlink privilege, runs on ubuntu CI). |
| R-03 | S3 (doc) | negscore | `NEGATIVE-SCORER.md` §2 still said the v3 constants live in `evals/negscore.py`; they moved to `sidecar/negscore.py` (the file itself already says so on line 3). | **Fixed** — doc now names `sidecar/negscore.py` + shim. |
| R-04 | S3 (doc) | foldfork | Idempotence wording did not state the consequence of a changed source `content_hash` (new `block_id` → new fact appended, stale fact stays). Verified by experiment (`appended: 1, deduped: 1`, count 5 → 6). | **Fixed** — `MMU-FOLD-FORK.md` now states idempotence is per source content and a changed source appends a new fact (append-only, not an update). |
| R-05 | S2–S3 (residual) | cow_forker | The RBAC re-filter covers **Tier 1 only** (spec `Module-Implementation.md:98` mandates exactly that). A role-denied ledger block evicted to Tier 2 as an overflow handle still reaches the child as `[swap] <8-word summary> (ctx://block/<id>)` because the fork no longer has the compile-time block set (the RBAC fields are not in the packet). `conflict_alerts` are likewise copied verbatim and can name denied ids. | **Not fixed (documented)** — a real fix requires passing the pre-policy block set into `fork_packet` (signature/semantic change beyond this wave's spec). Mitigation if it matters: resolve `ctx://block/<id>` handles against the store at fork time and re-apply `apply_policy`. |
| R-06 | S3 (process) | negscore | Pre-consolidation `evals/negscore.py` never existed in git (`git show HEAD:evals/negscore.py` → fatal); the old file was overwritten in the working tree, so v3 could not be byte-diffed against its pre-move copy. | **Not fixable** — v3 verified instead against the pre-registration doc constants and the F1–F6 flip tests; v1/v2 equivalence fully proven against HEAD. |
| R-07 | S4 (nit) | adapters | A hand-built packet with no renderable parts yields `{"system": []}` (Anthropic would reject an empty system array). Unreachable through `compile_packet` (the instruction block is always present and non-empty), so left as-is. Invalid TTL is also silently ignored when there is no cartridge — documented behavior, and the parameter is typed `Literal`. | **Not fixed (documented)**. |
| R-08 | S4 (limitation) | repofixture | Duplicate symbol names across files make an objective's answer key ambiguous: round-robin selection keys one file, but the other file is equally correct (verified with a two-file `shared` fixture). Scoring is information-presence, not answer quality (already stated in the receipt notes). | **Not fixed (documented)**. |
| R-09 | S4 (limitation) | repofixture | `_MAX_FILE_TOKENS` is checked after tokenizing the whole file, so an absurdly large `.py` (hundreds of MB) is fully tokenized before being skipped (time/memory on a local CLI only). | **Not fixed (documented)**. |

## Fixes applied (uncommitted)

1. `sidecar/repofixture.py` — containment guard in `_scan` (R-02), graceful `OSError` skip in `_select` (R-01), scan-rule docstring updated.
2. `tests/test_repofixture.py` — two regression tests (unreadable file; symlink escape).
3. `docs/engineering/NEGATIVE-SCORER.md` — constant location corrected (R-03).
4. `docs/engineering/MMU-FOLD-FORK.md` — idempotence-per-content clarified (R-04).

## Commands run

| Command | Result |
|---------|--------|
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 155 files already formatted |
| `uv run mypy protocol evals kernel sidecar adapters` | Success — no issues in 62 source files |
| `uv run python scripts/check_docs_numbers.py` | OK — 56 checks passed |
| `uv run python -m pytest -q` (baseline, before fixes) | 329 passed, 2 skipped (331 collected) |
| `uv run python -m pytest -q` (after fixes) | **330 passed, 3 skipped (333 collected)** — the extra skip is the symlink test on Windows; it runs on ubuntu CI |
| `uv run python -m pytest tests/test_bench_sidecar.py -q` ×3 | 1 passed each, no flakes |
| `run(concurrency=(1,4), requests=3)` + stderr capture | 0 errors, overhead delta emitted, stderr empty (no manifest dedupe) |
| Adversarial smokes (temp scripts) | adapters edge packets (15 cases) all OK; shadow-cortex unicode/weird input all OK; fork with denied/restricted/expired roles (math + identity + CLI parity) OK; fold idempotence + changed-content append (count 5→6) OK; repofixture binary null-byte `.py`, syntax-error `.py`, dir named `*.py`, unreadable (mocked), duplicate symbols, 0-symbol OK; negscore v1/v2 runtime equivalence vs HEAD over 2,001 cases OK |

## Residual risks (ranked)

1. **R-05 (tier-2 handle leak)** — partial RBAC disclosure to children under Tier-1 overflow; spec-compliant but worth a follow-up ticket.
2. **R-09 (giant-file tokenization)** — potential slow/high-memory skip on pathological repos.
3. **R-08 (ambiguous symbol keys)** — fixture quality, not correctness; scoring caveat already documented.
4. **R-07 (`system: []` / ignored TTL)** — adapter-only, unreachable from the compiler.
5. **Out of scope for this review:** `evals/pilot.py`, `evals/fixtures/`, and the PILOT docs landed in the same uncommitted pile but are outside the stated wave; `sidecar/costs.py` was reviewed (exact arithmetic, additive receipt keys) since `cfx demo --price` wires it in.

## Test count

**333 collected — 330 passed, 3 skipped** on Windows (2 embedding tests need the optional extra, 1 symlink test needs privilege). On ubuntu CI: 331 passed, 2 skipped expected.
