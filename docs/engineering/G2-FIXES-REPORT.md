# G2 — Audit-fix report (whole-repo, excluding sidecar server + B8 harness)

Audit-fix worker (internal task and dispatch identifiers removed).
Scope honored: edited only `protocol/`, `kernel/` (not `kernel/store/sqlite_store.py`),
`adapters/` (unchanged), `evals/` (not `b8_live.py`/`b8_aggregate.py`), `sidecar/repro.py`,
`scripts/`, `pyproject.toml`, `uv.lock`, `README.md`, `docs/`, `tests/` (not `test_b8*.py`,
`test_sidecar.py`), plus the three packaging files the dev-dependency move required
(`.github/workflows/ci.yml`, `CONTRIBUTING.md`, `CHANGELOG.md`). Nothing committed or pushed.

## Gates (all green)

| Gate | Result |
| :--- | :--- |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 224 files already formatted |
| `uv run mypy protocol evals kernel sidecar adapters` | no issues in 83 source files |
| `uv run python -m pytest -q` | 481 passed, 6 skipped |
| `uv run python scripts/check_docs_numbers.py` | 99 checks passed |
| `uv run cfx repro` | 99 offline checks passed |

## Fixes

| Finding | Fix | file:line | Test |
| :--- | :--- | :--- | :--- |
| AU3 L12 | drop redundant `.encode("utf-8")` on str literals | `protocol/canonical.py:146,148` | `ruff check` + `python -m protocol.canonical` |
| AU3 L2 | document the repr-vs-Ryū float-formatting limit (no canonical-output change) | `protocol/canonical.py:7-9,49` | `python -m protocol.canonical` |
| AU3 L3 | extract shared BM25 core (`BM25_K1/BM25_B/idf/bm25_core`) | `kernel/router/_bm25.py` (new); `recall.py`, `skim.py` | `tests/test_recall.py`, `tests/test_skim.py` |
| AU3 L4 | rewrite NK predicate as 3 explicit booleans + shared `tokenize(objective)` | `kernel/router/dependency_closer.py:58-68` | `python -m kernel.router.dependency_closer`, `tests/test_kernel.py` |
| AU3 L5 | MetricsScraper records `scrape_failed` + logs instead of silent zero | `evals/harness.py:52-62` | `tests/test_harness.py` (new, 3 tests) |
| AU3 L8 | capability `test` fields point at concrete methods (prefix sharing, CoW) | `scripts/check_docs_numbers.py:53,63` | `tests/test_docs_numbers.py` |
| AU3 L13 | add mcp.py branch coverage (pubkey missing/invalid, key_id mismatch, excerpt truncation, non-str protocolVersion) | `tests/test_mcp.py:301-371` (4 tests) | `tests/test_mcp.py` |
| AU3 M2 / AU4-06 / AU5-06 | one canonical test count (486 collected / 481 pass + 6 skip) + `stale_patterns` for `338 tests`/`tests-476` | `README.md:11,104,147`; `NUMBERS.md:45,483,stale_patterns` | `check_docs_numbers.py` |
| AU3 M3 | perf budget tolerant of slow/covered CI (250→2000 ms, 50→200 ms) | `tests/test_perf.py:96-97` | `tests/test_perf.py` |
| AU4-02 | ASCII-escape `cfx repro` output (cp1252 `→`/`×` crash) | `sidecar/repro.py:34-51` | `tests/test_repro.py` |
| AU4-03 | correct stale langgraph docstring | `tests/test_langgraph_parity.py:6-8` | — (doc) |
| AU4-04 | pin block timestamps so `content_hash` is stable across runs | `evals/spike_deterministic.py:41-53` | `python -m evals.spike_deterministic` |
| AU4-05 / AU5-04 | move dev tools to `[dependency-groups] dev` so bare `uv run` keeps them | `pyproject.toml:16-22`, `uv.lock`, `.github/workflows/ci.yml:16`, `CONTRIBUTING.md:8` | `uv run cfx repro`, bare `uv run pytest` |
| AU4-07 | REPRODUCE repo-fixture drift (7 files / 9,325 tokens / `cfx_repo_af83f34b`) | `docs/engineering/REPRODUCE.md:50` | `cfx demo --repo . --mock` |
| AU4-08 | REPRODUCE Tier-3 adds mechanical `--fabric mech` (13.62×) as headline | `docs/engineering/REPRODUCE.md:74-87` | — (doc) |
| AU5-05 (doc) | README states precisely that `cfx serve` does **not** mount cartridges/relations | `README.md:92-94,127-132` | — (doc) |
| AU5-07 | register B4/B4-v3/A4 headline numbers in NUMBERS (display-only + artifact) | `NUMBERS.md` (4 entries) | `check_docs_numbers.py` |
| AU5-08 | COSTS/REPRODUCE/NUMBERS lead with mechanical pilot (13.62×, 14,193 tokens) | `COSTS.md:121-124,147-149,165` | — (doc) |
| AU5-10 | Prod-Intro: qualify Zero-Token / CoW / >88% KV / CrewAI-AutoGen-A2A | `docs/product/Prod-Intro.md:42,43,45,65` | — (doc) |
| AU5-11 | CHANGELOG: `kernel/mmu/` is wired as `cfx fold`/`cfx fork` (not "not yet wired") | `CHANGELOG.md:9-12` | — (doc) |
| AU5-12 | index 18+ missing docs in the docs index | `docs/README.md` | — (doc) |
| AU4-14 (partial) | Ubuntu 22.04 → 26.04 LTS | `RUNTIME.md:12`, `DOGFOOD.md:3` | — (doc) |
| AU2 L1 | `tar --no-same-owner` + digest-pinning caveat | `scripts/wsl_b8_extract.sh:9-13` | — (shell) |
| AU2 L2 | caveat the `curl | sh` uv installer (pin/verify alternative) | `scripts/wsl_setup_vllm.sh:11-17` | — (shell) |
| C4 | README "Where we are" status table (proven / failed / in-progress / next) | `README.md:84-100` | `check_docs_numbers.py` |

## Findings left (not in this worker's files)

Owned by the sidecar-server worker or B8-harness worker, or excluded by the DO-NOT-TOUCH list:

- **AU3:** H1 (streaming ledger — `sidecar/server.py`), M1 (ledger lock/race — `b8.py`/`server.py`/`b8_live.py`), M4 (shared SQLite — `server.py`/`sqlite_store.py`), L1 (`confidence` inf — `sidecar/blocks.py`), L6/L7 (`b8_live.py`), L9 (`server.py` httpx close), L10 (`render.py` CF_RECAP), L11 (`obsfold.py` stub id), and the `_FRAME_TOKENS` half of L3 (`b8.py`/`obsfold.py`).
- **AU4:** AU4-01/09/10/11/12/13 (`b8_live.py` + B8 ops), AU4-14 `B8-HARNESS.md` (frozen `B8-*.md`), AU4-15 (`.gitignore` — DO-NOT-TOUCH), AU4-16/17 (INFO).
- **AU5:** AU5-01/02/03 (`b8_live.py`), AU5-09 (`B8-*.md` + `b8_live.py`), AU5-14 (`server.py` watermark), and the AST-parse half of AU5-13 (root cause — test-import failures — is resolved by the dev-group move; the ast-parse refactor was judged not worth the churn).
- **AU2 (security):** H1/H2/M1 (`server.py` CORS/trusted-flag/413-drain), M2 (`cli.py` seed on argv), M3 (untrack live B8 artifacts — `.gitignore`/git hygiene, DO-NOT-TOUCH + operational).

## Notes / risks

- SKIM.md and SKIM-MODEL.md are self-labeled "EXPLORATORY — nothing in this file is a claim of record", so their numbers were deliberately not registered in NUMBERS.md (they are not claims).
- The repo-fixture (`cfx demo --repo . --mock`) is drift-prone by construction (walks `*.py` to a token target); REPRODUCE.md now says so rather than pinning a subdirectory.
- I edited `.github/workflows/ci.yml` and `CONTRIBUTING.md` only because moving `dev` to a dependency group makes the old `--extra dev` flag an error; `ci.yml` now passes `--extra langgraph` (the `dev` group is installed by default).
