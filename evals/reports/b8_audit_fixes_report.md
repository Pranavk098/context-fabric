# B8 harness fixes — build report (audit AU1 + AU3)

Pilot restarts as v3 after these land; the pre-registration is written separately by the
coordinator. All gates green: `ruff check .` clean, `ruff format --check .` clean,
`mypy protocol evals kernel sidecar adapters` clean, `pytest -q` → **492 passed, 6 skipped**,
`check_docs_numbers.py` → 89 checks. No live API calls, no vLLM, Docker untouched.

## Per-finding changes and tests

| Finding | Change (file:line) | Test proving it |
|---|---|---|
| F1 CRITICAL shared task header | `evals/b8_live.py:280` `build_task_header` (problem + query + notes, delimited), `:295` `build_subagent_task`; `run_subagent` takes the combined task | `test_task_header_identical_across_arms` |
| F2 HIGH BM25 parent listing + provenance | `evals/b8_live.py:414` `_bm25_file_listing` (ranked `path: signature` within 4,000 tokens), `:509` `localize_parent` records `listing_shown`/`prompt_tokens`/`prompt_sha256`, `:597` `parent_handoff` adds `model`/`created_at`/`harness_sha` | `test_bm25_file_listing_ranks_and_signs`, `test_parent_cache_records_provenance` |
| F3 HIGH pair-reserve | `evals/b8_live.py:60` `RUN_RESERVE = 550_000`, `:1060` `pair_fits`, checked before each task × arm | `test_pair_fits_reserves_before_starting` |
| F4 `_print_report` KeyError | `evals/b8_live.py:1320` filters rows without `tokens` and non-results | `test_print_report_skips_tokenless_rows` |
| F5 uniform patch recovery | `evals/b8_live.py:656` `recover_patch` (agent submission → working-tree `git diff` → empty), `:672` `run_subagent`; `patch_source` recorded | `test_recover_patch_is_uniform_across_exit_statuses`, `test_salvage_git_diff_reads_working_tree` |
| F6 aggregator | `evals/b8_aggregate.py` (new): R/O/U + exact McNemar, excludes cap_stopped/parent_failed/tooling_failure | `test_aggregate_excludes_non_results`, `test_mcnemar_exact` |
| F7 fold stats + sidecar errors | `sidecar/server.py:488` counts `folded_events`/`folded_bytes` in `_obs_fold`; `evals/b8_live.py` `_start_sidecar` redirects stderr to a log + fails fast on a bound port, `_sidecar_stats` reads per-run deltas | `test_fold_stats_surfaced_in_sidecar_stats` |
| F8 window-cap estimate | `sidecar/b8.py:48` `_FRAME_TOKENS = 8`, `:66` `estimate_messages_tokens(..., tools=...)` adds the serialized tools schema | `test_window_cap_estimate_counts_tools_and_framing` |
| F9 de-duplicate candidates | `evals/b8_live.py:509` `dict.fromkeys` before BM25 gap fill | `test_parent_dedupes_candidate_files` |
| F10 UTC day per pair | `evals/b8_live.py` `main` computes `day = b8.utc_day()` inside the pair loop | `test_utc_day_boundary` |
| F11 atomic cap check-and-reserve | `sidecar/server.py:213` `gate_lock`, `:333` held across gate + proxy + record; `sidecar/b8.py:169` `summary`/`day_total` take `self._lock` | `test_ledger_reads_and_appends_are_thread_safe` |
| F12 persistent JSONL + one re-run | `evals/b8_live.py:846` `append_run` (crash-safe JSONL), `:857` `tooling_failures`, `--rerun-tooling-failures` flag | `test_tooling_failures_latest_row_wins` |
| F13 day-2 report wording | `evals/reports/b8_void_20261020/b8_pilot_run_20261020.md` — 13279 "applied, but broke `cache.py` syntax … graded unresolved" | (doc change, no test) |
| F14 parent provenance + cap not cached | `evals/b8_live.py:597` `parent_handoff` never caches `cap_stopped`; provenance fields recorded | `test_parent_cap_stopped_is_not_cached`, `test_parent_cache_records_provenance` |
| F15 pin revision + assert sample | `evals/b8_live.py:68` `DATASET_REVISION`, `:340` `load_instances` pins it, `:360` `assert_frozen_sample` | `test_assert_frozen_sample_guards_drift` |
| AU3-H1 refuse stream | `sidecar/server.py:528` `stream_not_ledgered` 400 when the ledger is on | `test_stream_rejected_when_ledger_on` |
| AU3-M1 record before write | `sidecar/server.py:683` `_record_usage` before `wfile.write` | `test_daily_cap_records_usage_after_proxy` |

## Offline gold-file visibility (new BM25 parent listing, 4,000-token budget)

Computed offline (HF_DATASETS_OFFLINE=1, pinned revision, `~/b8_repos`). "gold in shown" =
whether any file from the dataset's `patch` field appears in the ranked listing the parent
sees. The gold patch is never shown to any model.

| task | #py | shown files | gold file(s) in patch | in shown listing |
|---|---|---|---|---|
| django__django-11532 | 2565 | 10 | message.py, utils.py, validators.py, encoding.py, html.py | **yes** — message.py (rank 2), validators.py (rank 4) |
| django__django-12262 | 2588 | 10 | template/library.py | **yes** — rank 1 |
| django__django-13279 | 2669 | 23 | sessions/backends/base.py | **yes** — rank 10 |
| django__django-14915 | 2702 | 11 | forms/models.py | no |
| django__django-15851 | 2757 | 21 | db/backends/postgresql/client.py | no |
| matplotlib__matplotlib-26208 | 908 | 10 | lib/matplotlib/axes/_base.py | no |
| scikit-learn__scikit-learn-14496 | 733 | 27 | sklearn/cluster/optics_.py | **yes** — rank 1 |
| sphinx-doc__sphinx-8035 | 510 | 28 | sphinx/ext/autodoc/__init__.py | **yes** — rank 2 |
| sympy__sympy-12481 | 1115 | 14 | sympy/combinatorics/permutations.py | **yes** — rank 1 |
| sympy__sympy-19346 | 1398 | 17 | sympy/printing/repr.py | no |

6/10 tasks now show the gold file to the parent (vs 3/10 with the old alphabetical prefix);
django-12262 — the task the audit flagged as "gold absent" — now ranks it #1.

## New hand-off sizes (header + 8,192-token slot), the 10 tasks

Header = problem statement + sub-agent query + parent notes (identical across A/B/C, not
counted in the slot). Slot sizes are the A/B/C hand-off token counts. Header figures use the
day-1 (voided) parent query/notes; v3 parents are regenerated with the BM25 prompt, so the
exact header count will shift slightly, but the structure and slot accounting are unchanged.

| task | header tokens | slot | arm A | arm B | arm C |
|---|---|---|---|---|---|
| django__django-11532 | 821 | 8192 | 8189 | 3608 | 8183 |
| django__django-12262 | 370 | 8192 | 8189 | 2962 | 8149 |
| django__django-13279 | 244 | 8192 | 6112 | 1532 | 6513 |
| django__django-14915 | 384 | 8192 | 8189 | 4897 | 8171 |
| django__django-15851 | 260 | 8192 | 8190 | 2620 | 8171 |
| matplotlib__matplotlib-26208 | 771 | 8192 | 8189 | 4472 | 8181 |
| scikit-learn__scikit-learn-14496 | 739 | 8192 | 8189 | 3993 | 8172 |
| sphinx-doc__sphinx-8035 | 269 | 8192 | 8189 | 4660 | 8176 |
| sympy__sympy-12481 | 191 | 8192 | 8189 | 3011 | 8177 |
| sympy__sympy-19346 | 261 | 8192 | 8189 | 4875 | 8169 |

## Archive

Void v2 day-1 artifacts moved with `git mv` into `evals/reports/b8_void_20261020/`:
10 parent caches (`evals/parent_handoff_raw/*` → empty), `b8_predictions.jsonl` and
`b8_predictions.A.jsonl`, `b8_pilot_run.log`, `b8_pilot_run_20261020.md`,
`gpt-4.1-mini.b8_A.json`, and `b8_pilot_reports/b8_A_django__django-13279.json`. Ledger files
kept in place.

## Notes

- `mypy` is run as `python -m mypy` (the `mypy` shim is blocked by a Windows Application
  Control policy on this host).
- `tooling_failure` rows are also excluded from the aggregator's R/O/U (a tooling failure is
  re-run via `--rerun-tooling-failures`, so it is not a finished result).

## Coordinator addendums (AU4 deployment + AU5 claims)

- **(a) obs store checkpoint/close (AU5).** `kernel/store/sqlite_store.py:410` `close()`
  checkpoints the WAL (`PRAGMA wal_checkpoint(TRUNCATE)`) and closes the connection;
  `sidecar/server.py:830` closes both stores in `serve()`'s `finally`; `evals/b8_live.py:998`
  `_stop_sidecar` sends SIGINT first so the sidecar shuts down gracefully (checkpoint runs).
  F7 fold counts are written straight into the run JSONL (`_run_row` → `fold_events`/
  `folded_bytes`), never read back from `obs.db`. Tests: `test_sqlite_store_close_checkpoints_wal`,
  `test_fold_stats_surfaced_in_sidecar_stats`.
- **(b) arm B cartridge note.** `docs/engineering/B8-HARNESS.md` now states arm B's compiled
  packet mounts `_repo_cartridge` (the Tier-0 hologram), same as arm C.
- **(c) fresh fold store + untrack obs.db (AU4-11/15).** `evals/reports/b8_ledger/obs.db`
  archived to `evals/reports/b8_void_20261020/obs.db` with `git mv`; added
  `evals/reports/b8_ledger/*.db` to `.gitignore` so a future `obs.db` is untracked (the
  `tokens-*.jsonl` ledgers stay tracked). The runner recreates a fresh `obs.db` on the next
  arm-C run.
- **(d) transient exception → tooling failure (AU4-13).** `evals/b8_live.py:648`
  `_classify_exception` maps any non-cap, non-overflow exception (network/API/Docker fault)
  to `tooling_failure`, which is never written to predictions and is eligible for the single
  `--rerun-tooling-failures` re-run. Test: `test_transient_exception_is_tooling_failure_not_finished`.

