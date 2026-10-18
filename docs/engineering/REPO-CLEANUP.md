# REPO-CLEANUP — Repository Hygiene Audit

**Date:** 2026-10-18 · **Auditor:** repo-hygiene agent · **Commit base:** `0e65c6f`
**Scope:** storage hygiene (dead files, misplaced files, packaging, gitignore/CI/config correctness, light dead-code sweep).
**Untouched by design:** `README.md`, `docs/` content (concurrent doc rewrite — only this new file was added), website assets.
**Not committed** (per instruction): all changes are in the working tree.

**Verification run during this audit** (details in §4):

| Check | Result |
| :--- | :--- |
| `uv run pytest -q` (local, fastembed present) | 226 passed |
| Isolated CI-parity env (`uv sync --extra dev`, no fastembed) | 224 passed, 2 skipped (embedding tests skip gracefully) |
| `ruff check .` / `ruff format --check .` (CI-parity env) | clean / 125 files formatted |
| `mypy protocol evals kernel sidecar` (CI-parity env) | no issues in 51 source files |
| `python -m evals.spike_deterministic` (CI step) | ALL CHECKS PASS |
| `uv build` (wheel) | builds; wheel now `protocol`, `kernel`, `sidecar` only |
| `docker compose -f docker-compose.yml config` | rc=0 (file is valid) |
| `python -m kernel.mmu.epoch_folder` / `cow_forker` | both self-checks OK |

---

## 1. Executed changes (safe fixes)

| # | Change | Evidence / rationale |
| :- | :--- | :--- |
| 1 | **Deleted 5 dead scripts** (see §2.1): `wsl_install_vllm.sh`, `wsl_start_service.sh`, `wsl_status.sh`, `wsl_debug_cache.sh`, `wsl_metric_names.sh` | Each is unreferenced anywhere in the repo (grep on filename) and superseded or one-off; see per-file evidence below. |
| 2 | **`.gitignore`: added `demo_receipt.json`, `epochs.json`, `*.db`** | `cfx demo` defaults `--out demo_receipt.json` in cwd (`sidecar/demo.py:872`); `cfx seal --out epochs.json` and `--store fabric.db` (`README.md:78-81`, `docs/engineering/SEAL.md:101-102`); `python -m kernel.store … <db>` takes any `*.db`. None were ignored; no tracked file matches these patterns (`git check-ignore` verified, 0 tracked matches). |
| 3 | **`pyproject.toml`: wheel packages `["protocol", "evals", "kernel", "sidecar"]` → `["protocol", "kernel", "sidecar"]`** | `evals` is dev-only experiment code (spikes, audit harnesses, GPU benchmarks), not a product package. Verified: `uv build` succeeds; wheel zip now contains only `protocol/ kernel/ sidecar/`; the sdist still includes `evals/` and `tests/` (correct for a source distribution); `uv run` executes from source, so local dev and CI are unaffected. Tests confirm: 226 passed after the change. |
| 4 | **`CONTRIBUTING.md`: quality-gate line `uv run mypy protocol evals` → `uv run mypy protocol evals kernel sidecar`** | CONTRIBUTING lagged the Makefile (`Makefile:22`) and CI (`ci.yml:22`), which both typecheck all four packages. Verified mypy passes on the full scope. |
| 5 | **Stale doc path fixed in 6 eval files + `docker-compose.yml` comment**: `docs/STAGE-B-RUNTIME.md` → `docs/engineering/RUNTIME.md` | `docs/STAGE-B-RUNTIME.md` does not exist anywhere; the live file is `docs/engineering/RUNTIME.md` (also cited by `CONTRIBUTING.md:14`). Occurrences: `evals/stage_b2.py:25,174`, `stage_b3.py:10,94`, `stage_b4.py:149`, `stage_b5.py:162`, `audit_fixes2.py:319`, `spike_kv_cache.py:126`, `docker-compose.yml:33`. Post-fix grep for `STAGE-B-RUNTIME`: zero hits. |

Working tree after the audit (excluding the concurrent docs-editor changes): `.gitignore`, `CONTRIBUTING.md`, `docker-compose.yml`, `pyproject.toml`, 6 eval files modified; 5 scripts deleted; `docs/engineering/REPO-CLEANUP.md` added.

---

## 2. Findings

### 2.1 `scripts/` (9 files → 4)

| File | Verdict | Evidence |
| :--- | :--- | :--- |
| `wsl_run_eval.sh` | **KEEP** | Canonical experiment runner, cited in `ENGINEERING-LOG.md:108,180,570,580` (`scripts/wsl_run_eval.sh evals.stage_b2` etc.), `PACKING-QUALITY.md:157`, and referenced by 6 eval modules' error messages. Matches the logged runtime how the experiments were actually run: `--enable-prefix-caching --max-model-len 8192 --enforce-eager --gpu-memory-utilization 0.85` vs `ENGINEERING-LOG.md:10-11` (same flags); sets `CF_BASE_URL`/`CF_MODEL`/`PYTHONPATH` and warms the engine. **Confirmed correct.** |
| `wsl_run_spike.sh` | **KEEP** | Cited `ENGINEERING-LOG.md:13`: "Runner: scripts/wsl_run_spike.sh (start server → wait ready → run evals.spike_kv_cache → stop)". Behavior matches. |
| `wsl_setup_vllm.sh` | **KEEP** | The only remaining script that creates `~/.venvs/vllm` — which both canonical runners require (`VENV="$HOME/.venvs/vllm"`, `"$VENV/bin/python"`). Removing it would make the cited runs unreproducible. Unreferenced by docs (runbook is in its header). |
| `wsl_probe.sh` | **KEEP (listed)** | Unreferenced by docs, but a still-functional, recurring pre-flight diagnostic (torch/CUDA/vLLM/disk). Produced the `RUNTIME.md` §0 "Hardware reality" table class of output. Kept; delete if the orchestrator wants the script set trimmed to the two cited runners. |
| `wsl_install_vllm.sh` | **DELETE** | Install-only subset of `wsl_setup_vllm.sh` (same uv/py3.12/venv/vllm steps); unreferenced anywhere. |
| `wsl_start_service.sh` | **DELETE** | systemd-user-service approach abandoned: `wsl_run_eval.sh:14-16` and `wsl_run_spike.sh:14-16` explicitly `rm -f …/cf-vllm.service` and `systemctl stop` it before every run. Unreferenced. |
| `wsl_status.sh` | **DELETE** | Only meaningful for that abandoned `cf-vllm.service` unit. Unreferenced. |
| `wsl_debug_cache.sh` | **DELETE** | One-off diagnosis of how vLLM reports prefix-cache hits; its answer is permanently encoded in `evals/harness.py` (scrape `/metrics`) and recorded in `ENGINEERING-LOG.md:24-27,101`. Unreferenced. |
| `wsl_metric_names.sh` | **DELETE** | One-off metric-name discovery; findings recorded in `ENGINEERING-LOG.md:253,600` (`vllm:kv_cache_usage_perc`). Unreferenced. |

### 2.2 `evals/` (16 modules + 12 tracked reports)

All modules map to a role and are referenced by docs — **nothing deleted here** (historical scripts are the evidence trail for published numbers).

| File | Role | Referenced by |
| :--- | :--- | :--- |
| `harness.py` | shared HTTP client / metrics scraper / token counter | imported by all stage/audit/spike modules |
| `spike_deterministic.py` | zero-GPU deterministic-core spike | `Makefile:det`, `ci.yml:26`, `VERIFICATION-REPORT.md:6`, `tests/test_protocol.py:7` imports it |
| `spike_kv_cache.py` | Stage B KV-cache falsification spike | `ENGINEERING-LOG.md:13`, `RUNTIME.md:3,52` |
| `spike_epoch_determinism.py` | cross-process byte-equality check | `SEAL.md:60,149,164` |
| `stage_b2.py` … `stage_b5.py`, `stage_b7.py` | staged GPU experiments (B2 fork cost … B7 arbitrage) | `ENGINEERING-LOG.md:107-252,522`, `VERIFICATION-REPORT.md` N3/N4/N10/N21 |
| `audit_fixes.py`, `audit_fixes2.py` | audit-fix rounds 1 and 2 | `ENGINEERING-LOG.md:294,402`, `VERIFICATION-REPORT.md` N1/N2/N8/N9/N11/N12/N13. `audit_fixes2.py` is explicitly documented as **historical evidence, keep** (`RERUN-REVIEW.md:136-141`: "The audit_fixes2 variant is historical only … left [in place]"). |
| `audit_b6.py` | cold-boot / real-repo / quality / negative-knowledge section | `ENGINEERING-LOG.md:487,575`, `VERIFICATION-REPORT.md` N5/N6/N7/N14 |
| `packing_bench.py` | offline packer benchmark | `PACKING-QUALITY.md:48,156`, `GAP-WAVE-MEASUREMENTS.md:28` |
| `position_probe.py` | rule-position GPU probe | `PACKING-QUALITY.md:82-88,157`, `tests/test_position_probe.py` |
| `recall_bench.py` | BM25/embeddings/hybrid recall benchmark | `RECALL-BENCH.md:4`, `GAP-WAVE-MEASUREMENTS.md:27`; report force-added |
| `__init__.py` | package marker (required by all `evals.` imports) | — |

**Tracked reports note:** 12 evidence JSONs are force-added under the gitignored `evals/reports/` (not 7). `RERUN-REVIEW.md:157-158` documents the force-add intent ("to preserve the re-run evidence, force-add `audit_b6_20261017_124755.json`, `stage_b5_20261017_124856.json`, `audit_fixes_20261017_115748.json`…"). **Kept intentionally.** See recommendation R1 for cited-but-untracked reports.

### 2.3 Root files

| File | Verdict | Evidence |
| :--- | :--- | :--- |
| `docker-compose.yml` | **KEEP, FIXED (comment)** | **It is NOT Postgres.** It is the Stage-B vLLM OpenAI-compatible GPU server (`vllm/vllm-openai`, `--enable-prefix-caching`, port 8000, `hf-cache` volume, NV GPU reservation, `ipc: host`), i.e. the "hardware ally" the spike needed. Documented by `RUNTIME.md:22-35` (preferred path) and referenced by `evals/spike_kv_cache.py:121`. Validated: `docker compose config` rc=0. Fixed one stale comment path (§1.5). **Correction to the audit brief:** it does *not* match `docs/design/Db-Storage-Spec.md` — that spec is the PostgreSQL canonical schema (prod target, mirrored by `kernel/migrations/001_initial_fabric_schema.sql`); the v0 store is SQLite (`kernel/store/sqlite_store.py`). `docs/design/Repo-Tech-Stack.md:20` describes compose as "PostgreSQL 16 (pgvector) + Sidecar" — stale/wrong (docs editor should fix). |
| `Makefile` | **KEEP, no changes** | Every target read and validated: `sync`(uv py3.12), `fmt`/`lint`/`test`(ruff/pytest), `typecheck` = `mypy protocol evals kernel sidecar` (**all 4 packages — correct**), `det` = `python -m evals.spike_deterministic`, `spike` = `python -m evals.spike_kv_cache`. `.PHONY` complete; every module path exists. No doc references a `make <target>` (grep: zero hits), so no target-name drift. |
| `.github/workflows/ci.yml` | **KEEP, no changes (2 optional nits)** | Validated against an isolated CI-parity env: Python 3.12 matches `requires-python >= 3.12`; `uv sync --extra dev` provides pytest/mypy/ruff; mypy scope matches Makefile; `pytest -q` → **224 passed, 2 skipped without fastembed** (embedding tests self-skip — the "fastembed-absent behavior" is CI-safe and pinned by `tests/test_recall.py`); `spike_deterministic` passes without GPU. Ubuntu-only is appropriate (GPU experiments cannot run on GH runners; they are not in CI on purpose). Nits: `astral-sh/setup-uv@v3` is old (bump when convenient); no `concurrency` cancel-in-progress group (optional). |
| `.gitignore` | **FIXED** | All cache/build entries present; only generated-artifact gaps were the three added patterns (§1.2). |
| `.gitattributes` | **KEEP, correct** | `* text=auto eol=lf` + per-type LF for `.py/.sh/.md/.yml/.toml/.json` — right for a Windows checkout of a Linux/WSL-targeted repo with executable `.sh` scripts. |
| `pyproject.toml` | **FIXED** | Wheel packaging (evals removed, §1.3). mypy strict + fastembed override correct; ruff config fine. |
| `CONTRIBUTING.md` | **FIXED** | mypy scope (§1.4). |
| `CHANGELOG.md`, `LICENSE`, `uv.lock` | **KEEP** | Expected; lockfile is the largest tracked file (372 KB) but must be tracked for reproducibility. |

### 2.4 `tests/` (21 files) — mapping, duplicates, orphans

No duplicates, no stragglers from renames, no orphaned helper modules (there is no `conftest.py` and none is needed). Every file maps to a source area:

| Test | Source under test |
| :--- | :--- |
| `test_kernel.py` | `kernel/cartridge/{serializer,ast_extractor}.py`, `kernel/router/compiler.py` |
| `test_packer.py` | `kernel/router/packer.py` |
| `test_recall.py` | `kernel/router/recall.py`, `kernel/router/embeddings.py` (2 tests skip w/o fastembed) |
| `test_mmu.py` | `kernel/mmu/{epoch_folder,cow_forker}.py` |
| `test_epochs.py` | `kernel/epochs.py` (COW `MemoryState`) — *not* a duplicate of `test_epoch.py` |
| `test_hints.py` | `kernel/router/hints.py` |
| `test_store.py` / `test_store_relations.py` | `kernel/store/sqlite_store.py` (blocks / relations — distinct surfaces) |
| `test_epoch.py` / `test_merkle_proofs.py` / `test_protocol.py` / `test_signing.py` | `protocol/epoch.py`, `protocol/models.py`, `protocol/canonical.py`, `protocol/signing.py` |
| `test_cli.py` / `test_replay.py` / `test_sidecar.py` / `test_mcp.py` / `test_render.py` / `test_demo.py` / `test_scorer.py` | `sidecar/cli.py`, `replay.py`, `server.py`, `mcp.py`, `render.py`, `demo.py` (scorer tests `sidecar/demo.py:is_bad_answer*`) |
| `test_perf.py` | cross-module perf gate (`compiler` compile + `sidecar.render`) |
| `test_position_probe.py` | `evals/position_probe.py` (eval-tool logic) |

Two cross-boundary imports worth knowing about (report only, both deliberate reuse): `tests/test_protocol.py:7` imports `build_packet` from `evals/spike_deterministic.py`, and `tests/test_position_probe.py` imports from `evals/position_probe.py`. Neither is a packaging problem (tests run from source).

### 2.5 Git hygiene

* **Tracked tree contains no caches or generated outputs**, except the intentionally force-added `evals/reports/*.json` evidence (see §2.2). `git status --ignored` shows only caches (`__pycache__/`, `.venv/`, tool caches) and the untracked report backlog.
* **`.gitignore` gaps fixed** (§1.2). New patterns verified with `git check-ignore`: `demo_receipt.json`, `epochs.json`, `fabric.db` all match; **zero tracked files** are matched by the new patterns (no accidental shadowing).
* **Top 10 largest tracked files** (no LFS/repo-size concern; largest is 372 KB, all text):

| # | Size | File |
| :- | ---: | :--- |
| 1 | 372.4 KB | `uv.lock` |
| 2 | 58.2 KB | `evals/reports/recall_bench_20261018T061752Z.json` |
| 3 | 36.1 KB | `sidecar/demo.py` |
| 4 | 34.4 KB | `docs/research/PLUG-PRIOR-ART.md` |
| 5 | 31.4 KB | `evals/reports/demo_negative_3b_20261017_232032.json` |
| 6 | 30.6 KB | `tests/test_sidecar.py` |
| 7 | 29.9 KB | `docs/engineering/ENGINEERING-LOG.md` |
| 8 | 29.8 KB | `docs/research/PATH-VIABILITY.md` |
| 9 | 26.8 KB | `docs/research/SHARED-MEMORY-PRIOR-ART.md` |
| 10 | 25.2 KB | `evals/reports/demo_negative_20261017_231935.json` |

No history rewrite needed or attempted (out of scope).

### 2.6 Misplaced files / name-vs-content

* **No misplaced code found.** `evals/` contains only experiment/bench code; `kernel/` contains no experiment code and never imports `sidecar`/`evals` (grep: zero matches); `protocol/` is network-free.
* Near-collision clarified: `protocol/epoch.py` (signed chained epoch *receipts*/Merkle, `SEAL.md`) vs `kernel/epochs.py` (COW `MemoryState` fork/fold, `SHARED-COPY.md`) — both names match their content; keep.
* `sidecar/replay.py` contains both `replay` and `seal` — matches `SEAL.md`'s documented entry point.
* `evals/` no longer ships in the wheel (§1.3) — the only true packaging-level "misplacement" found.

### 2.7 Dead-code sweep (light)

| Item | Verdict | Evidence |
| :--- | :--- | :--- |
| `kernel/router/hints.py` | **Wired** | Imported by `kernel/router/compiler.py:17` (`packet_cache_hints` — trace path) and `tests/test_hints.py:7`; documents `AGENT-HINTS.md:8`. |
| `kernel/mmu/*` | **Intentionally unwired — keep** | Not referenced by compiler/server; importable via `kernel/mmu/__init__.py`; runnable self-checks both pass: `python -m kernel.mmu.epoch_folder` → "self-check: OK (k=3 fold, tail appended, deterministic)"; `cow_forker` → "self-check: OK (shared tiers, fresh register, deterministic)". Benign `runpy` RuntimeWarning when invoked with `-m` (because `__init__` re-exports the submodules) — cosmetic, not fixed. |
| `kernel/router/dependency_closer.py` | **Wired** | `compiler.py:16`, `kernel/store/sqlite_store.py:28`, tests. |
| `kernel/router/policy_engine.py` | **Wired** | `compiler.py:19`. |
| `kernel/cartridge/pagerank_map.py` | **Wired** | `kernel/cartridge/serializer.py:16`. |
| `evals/audit_fixes2.py` | **Historical evidence — keep** | Documented superseded-but-preserved (`RERUN-REVIEW.md:136-141`). |
| `protocol/epoch.py` `__main__` selftest | Wired | `README.md:78`, `SEAL.md:59` CLI usage. |

No unused modules or exports found beyond the above; nothing deleted per instruction.

---

## 3. Packaging recommendation (executed)

**`evals` must not ship in the wheel; it should remain importable from source.** Executed:

```toml
[tool.hatch.build.targets.wheel]
packages = ["protocol", "kernel", "sidecar"]
```

* Rationale: `evals` is dev-only (GPU spikes, audit harnesses, synthetic corpora); it depends on optional/benchmark-only paths and nothing in `protocol`/`kernel`/`sidecar` imports it.
* Impact verified: `uv build` succeeds; wheel contents = `protocol/ kernel/ sidecar/` + dist-info only; **sdist still carries `evals/` + `tests/`** (normal and desirable for a source distribution); `uv run`/`pytest`/CI execute from source — full suite (226) and CI-parity suite (224+2 skipped) still pass post-change.
* The only consumer outside `evals/` itself is `tests/` (two files), which never ships in any artifact.

---

## 4. Recommendations for the orchestrator (not executed)

**R1 — Evidence-trail gap (highest value).** Several reports *cited as load-bearing evidence* in `ENGINEERING-LOG.md` / `VERIFICATION-REPORT.md` exist only on this machine (gitignored, untracked): `kv_spike_20261016_142753.json` (the Stage-B headline report!), `stage_b2_20261016_145513.json`, `stage_b3_20261016_150108.json`, `stage_b4_20261016_152935.json`, `stage_b5_20261016_153211.json` and `_153023.json` (wrong-gauge evidence), `audit_fixes_20261016_154540/154833.json`, `audit_fixes2_20261016_160822/161119.json`, `audit_b6_20261016_173635/173804/174056.json`, `stage_b7_*.json`. Consider force-adding the cited set (~13 files, each ≤ 60 KB) so the docs' claims remain verifiable after a clone; or accept docs-only evidence. (Kept as-is; no changes made.)

**R2 — Docs editor:** (a) `docs/design/Repo-Tech-Stack.md:20` wrongly describes `docker-compose.yml` as "PostgreSQL 16 (pgvector) + Sidecar" — it is the vLLM GPU server; the Postgres schema lives in `Db-Storage-Spec.md`/`kernel/migrations/` and is the prod target, while the v0 store is SQLite. (b) Stale doc-internal links observed: `AUDIT.md:177` → `docs/AUDIT-FIXES-RESULTS.md` (missing), `CHANGES-FROM-PROPOSAL.md:124` → `STAGE-B-FINDINGS.md` (missing). (c) `RUNTIME.md:55` says the spike report lands in `evals/reports/kv_spike_*.json` — consistent with R1.

*(All three docs-editor items were fixed the same day — `docs/engineering/DOCS-AUDIT.md`, 2026-10-18.)*

**R3 — Environmental note (operational, not a repo defect):** a live `cfx` process (PID 25328, `.venv\Scripts\cfx.exe`) holds the entry point, so `uv run` cannot refresh the editable install after the `pyproject.toml` change until it exits. Once stopped, run `uv sync --extra dev` once. All audit verification above was done successfully with `--no-sync` / isolated envs in the meantime.

**R4 — CI polish (optional):** bump `astral-sh/setup-uv@v3` to a current major; optionally add a `concurrency` group with `cancel-in-progress: true`. No matrix changes recommended (Windows/GPU jobs would not add signal; GPU evals are intentionally out of CI).

**R5 — `docker-compose.yml` (optional hardening):** `vllm/vllm-openai:latest` is a mutable tag — pin a version for reproducibility; the `VLLM_ATTENTION_BACKEND=FLASH_ATTN` default is a V0-era knob that newer images may ignore. The file validates and matches `RUNTIME.md`; left functionally unchanged (comment-only fix).

**R6 — `wsl_probe.sh`:** unreferenced diagnostic kept deliberately (§2.1); delete if the orchestrator prefers the minimal 2-runner script set (deletion is recoverable from git history).

**R7 — Benign warning:** `python -m kernel.mmu.<mod>` emits a `runpy` RuntimeWarning due to `__init__` re-exports. Harmless; only worth fixing if the `-m` self-check UX bothers anyone (would require restructuring exports).

---

## 5. Risk items / deltas from the audit brief

1. **The brief's "docker-compose matches Db-Storage-Spec" is incorrect** — compose is vLLM-only (Stage-B runtime), the Postgres spec is unimplemented-in-v0 (SQLite is). No compose/Postgres coupling exists today; if/when the Postgres schema lands, a `postgres` service can be added then.
2. **Deleted files are recoverable** via `git checkout HEAD -- scripts/<name>.sh`; evidence for each is in §2.1. Nothing referenced by docs, CI, tests, Makefile, or any source file was deleted.
3. **The concurrent docs rewrite touched files during this audit** (`AUDIT.md`, `ENGINEERING-LOG.md`, and shortly after `PERFORMANCE.md`, `RUNTIME.md`, `SIDECAR.md`) — my edits never targeted `docs/` content; `REPO-CLEANUP.md` is a new file and cannot collide with their rewrites.
4. **Left in place (do not misread as oversight):** `evals/reports/` untracked backlog (intentional ignore), `evals/audit_fixes2.py` (historical evidence), `kernel/mmu/` unwired-by-design, the live `cfx` process (R3).
