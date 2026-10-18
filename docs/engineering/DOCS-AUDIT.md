# DOCS-AUDIT — Documentation Consistency Pass (2026-10-18)

Whole-docs consistency audit across every `*.md` in the repo (excluding `README.md`, which
the orchestrator is rewriting; excluding all code). Method: recompute every canonical metric
from the current artifacts and the code, grep every numeric claim, verify relative links,
run the gates (`pytest -q` → 226 passed; `tests/test_perf.py -s`), and reconcile every
discrepancy in place. Historical audit documents keep their numbers and gained supersession
banners instead of rewrites.

## 1. Canonical facts, re-verified against artifacts (not trusted from the brief)

| Fact | Value | Source (verified) |
| :--- | :--- | :--- |
| Demo fleet (broadcast) | 44,854 prompt tok / 6-6 / 2.80 s / KV peak 0.0605 | `evals/reports/demo_fleet_20261017_231929.json` (summed + ratios recomputed) |
| Demo fleet (compile) | 14,170 / 6-6 / 2.08 s / 0.0201; ratio 3.1655× → **3.17×** | same |
| Smoke cold | broadcast 83.1% hits (37,280/44,854), compile 0% (0/14,170) | `demo_fleet_smoke_20261017_231840.json` |
| Negative knowledge | 1.5B: control 2/2 bad, treated 1/2 v1 / 0/2 v2 — no clean effect; 3B: queue trap genuine compliance, metrics trap 2/2 → 0/2 both scorers | `demo_negative_20261017_231935.json`, `demo_negative_3b_20261017_232032.json` |
| Recall bench | lexical .340/.875/.289/.000; bm25 .340/.875/.859/.850; embeddings .380/.975/.889/.850; hybrid .380/.950/.882/.850 (P@5/R@10/MRR/top-1); hybrid does NOT beat embeddings-only | `recall_bench_20261018T061752Z.json` |
| Packing bench | greedy 27 / 0.2056 / 6-6 / 582, MMR 28 / 0.1780 / 5-6 / 583, −13.44% redundancy | `packing_bench_20261017_231759.json` |
| Position probe | HEAD/MIDDLE/TAIL 8/8/8 (null) | `position_probe_20261017_224320.json` |
| Cold boot (honest re-run) | 348 vs 3,146 tok, 100% vs 25% (n=4×3) | `audit_b6_20261017_124755.json` (cited; verified via RERUN-REVIEW reconstruction) |
| KV / throughput (latest) | KV peak 27.95% vs 1.38% = **20.2×**; 43.25 vs 5.26 req/s = **8.23×** | `stage_b5_20261017_124856.json` (recomputed: 0.279534/0.013823 = 20.222; 43.2502/5.2575 = 8.226) |
| Tests | **226 passed** (`uv run python -m pytest -q`, 51 s) | run in this audit |
| Perf | 5.01 / 14.33 / 61.65 ms compile (50/200/1000), 0.01 ms render | `tests/test_perf.py -q -s` run in this audit |

## 2. Discrepancies found and resolved

| # | Discrepancy | Where | Resolution |
| :- | :--- | :--- | :--- |
| 1 | Stale `8.6× / 44.8 / 5.2 req/s` (original 2026-10-16 run) presented as current | `CHANGES-FROM-PROPOSAL.md:147`, `COMPETITIVE-LANDSCAPE.md:136`, `ENGINEERING-LOG.md` J2, `SHARED-MEMORY-PRIOR-ART.md:133` (+ `README.md`:20 — **not touched by instruction**) | Replaced with **8.23× (43.25 vs 5.26 req/s)**; the original figures retained only where explicitly marked as the 2026-10-16 run. VERIFICATION-REPORT N3 row gained a supersede note. |
| 2 | `PERFORMANCE.md` 200-block cell 13.7 ms vs fresh 14.33 ms; "variance under 3%"; stale "~23 ms" in the language-boundary section | `PERFORMANCE.md` | Cell → 13.7–14.3 ms; variance → "a few percent" with the fresh 2026-10-18 session (5.01/14.33/61.65); "~23 ms" → "~61 ms" |
| 3 | B6.4 arbitrage "all four arms 100%" | `ENGINEERING-LOG.md` B6.4 | Corrected to artifact values (1.5B raw 93.3% / compiled 86.7%; 0.5B raw 80% / compiled 93.3%; N21) |
| 4 | "Also optional halving of the number of tokens" (no code path; N22) | `ENGINEERING-LOG.md` B6.1 | Removed |
| 5 | 15× tax "SUBSTANTIATED / not a rescope candidate" (N8: arithmetic identity, not a measured recovery) | `ENGINEERING-LOG.md` F8, `AUDIT.md` GAP-01 row | Downgrade annotations added in both; CHANGELOG already consistent |
| 6 | AUDIT bottom line: "only genuinely open item is GAP-08" while GAP-22 is OUTSTANDING | `AUDIT.md` | Reworded: open = GAP-08 + GAP-22 (harder set pending); ACCEPT = GAP-18 |
| 7 | 27.2× not marked synthetic-only; "real-repo run still pending" (it ran: 16.8× httpx) | `ENGINEERING-LOG.md` F7, `AUDIT.md` GAP-04 | Synthetic-only note + 16.8× real-code pointer (N9/N5) |
| 8 | "~1.4–2× throughput increment" (N20: F6 shows 1.41× increment, not a 1.4–2× range) | `CHANGELOG.md` findings, `AUDIT.md` headline reconciliation | Reworded to `~1.4× throughput increment from compression` (35.4 → 50.1 req/s, on top of sharing) |
| 9 | Broken pointers to merged docs: `docs/STAGE-B-FINDINGS.md` (×3), `docs/SHORTFALL-AUDIT.md` (×2), `docs/AUDIT-FIXES-RESULTS.md` (×2) | `CHANGES-FROM-PROPOSAL.md`, `ENGINEERING-LOG.md`, `AUDIT.md` | Point to `ENGINEERING-LOG.md` / `AUDIT.md` sections (the merge target named in `docs/README.md`) |
| 10 | `docs/adr/…` paths (dir is `docs/decisions/`) | `Arch-Spec.md`, `CHANGES-FROM-PROPOSAL.md` (10×) | Replaced with `docs/decisions/` |
| 11 | `kernel/db/migrations/…` path does not exist (actual: `kernel/migrations/…`) | `Build-Plan-Validation-Gate.md` | Path corrected |
| 12 | `usage.prompt_tokens_details.cached_tokens` recommended for vLLM cache read (null on vLLM 0.31); wrong metric name `vllm:gpu_prefix_cache_hit_rate` | `SIDECAR.md` A/B recipe, `RUNTIME.md` §1/§5 | Replaced with `/metrics` `prefix_cache_hits_total`/`prefix_cache_queries_total` + explicit null note |
| 13 | `--max-model-len 4096` in the recipe while the logged runs use 8192 (4096 overflows the 15-step loop) | `RUNTIME.md` §0 | Corrected to 8192 with the reason |
| 14 | 16.7% → 0% negative-knowledge headline without N14 caveat, now qualified by the regenerated demo set (1.5B null, 3B directional-only) | `COMPETITIVE-LANDSCAPE.md`, `AUDIT.md`, `ENGINEERING-LOG.md` B6.5 | Caveat + pointer to `DEMO.md` added |
| 15 | 9.5× role-divergent "strongest number" without the unequal-information caveat (N2) | `COMPETITIVE-LANDSCAPE.md:120`, `PATH-VIABILITY.md:141`, `SHARED-MEMORY-PRIOR-ART.md:133`, `CHANGELOG.md` findings | Caveat pointer added where the number is headlined |
| 16 | "artifacts are not part of the repo" / "git-ignored, not tracked" — 12 evidence JSONs are now force-added | `PACKING-QUALITY.md` (2 spots); historical versions in `RERUN-REVIEW.md`, `GAP-WAVE-MEASUREMENTS.md`, `DEMO-REVIEW.md` | PACKING-QUALITY updated to "tracked (force-added)"; historical docs bannered |
| 17 | Mojibake (double-encoded UTF-8: `Ã—`, `â€”`, `âœ…`, …) | `AUDIT.md` (50+), `ENGINEERING-LOG.md` (150+) | 17 sequences replaced; verified zero residual |
| 18 | Test-count snapshots (13 / 52 / 55 / 121 / 173) readable as current | `VERIFICATION-REPORT.md`, `RERUN-REVIEW.md`, `GAP-WAVE-*.md`, `EPOCHS-STORE-REVIEW.md`, `SIDECAR-REVIEW.md` | Banners added naming the current count (226, verified by running the suite); current count also present in `TRACK-CD-REVIEW.md` |
| 19 | Deleted/regenerated artifact pointers (`_2244xx` demo set, `231341` packing, `recall_…053623Z`) | `GAP-WAVE-MEASUREMENTS.md` | Banner: superseded by the `_2319xx` set / `231759` artifact with identical numbers |
| 20 | Docker-compose described as "PostgreSQL 16 + Sidecar" (it is the vLLM GPU server; Postgres is deferred) | `Repo-Tech-Stack.md` (flagged by `REPO-CLEANUP.md` R2a) | Corrected in the tree diagram; status note added to the doc |
| 21 | "will be implemented" drift in design-of-record docs (framework adapters, Shadow Cortex, Postgres, tree-sitter, reserve assertion, CoW-as-KV) | 8 design/product/research docs | Short dated (2026-10-18) status notes added, each pointing to `IMPLEMENTATION-STATUS.md` / `ROADMAP-DECISIONS.md` |
| 22 | `RUNTIME.md` Gate B read as an open decision | `RUNTIME.md` §5 | Outcome line added (passed 2026-10-16; pointers to §7–§8) |

## 3. History banners added (numbers kept verbatim)

- `VERIFICATION-REPORT.md` — historical snapshot banner; N3 row marked superseded (8.23×).
- `RERUN-REVIEW.md` — banner (numbers still canonical; test count and tracking status moved on).
- `GAP-WAVE-MEASUREMENTS.md` — banner (superseded demo/packing artifact set; 173 tests then).
- `GAP-WAVE-REVIEW.md` — banner (173 tests then; finding statuses unchanged).
- `EPOCHS-STORE-REVIEW.md` — banner (52/55 tests then).
- `SIDECAR-REVIEW.md` — banner.
- `DEMO-REVIEW.md` — already carried a banner (verified adequate; points at `DEMO.md` §Results).

## 4. Index updates (`docs/README.md`)

- Added: `IMPLEMENTATION-STATUS.md` (spec-vs-code map, starred) and `ROADMAP-DECISIONS.md`
  moved up beside it (duplicate lower entry removed).
- Added: `REPO-CLEANUP.md`, `DOCS-AUDIT.md`.
- Verified: every entry now resolves to an existing file; every current doc is listed
  (product 1, design 6, research 8, engineering 31, decisions 5).

## 5. Approach-wording consistency (check #2), verified

- Determinism framed as packet/state-level only; model answers explicitly out of scope
  (`SEAL.md`, `SHARED-COPY.md`, demo caveats) — consistent.
- "quarantine = draft lifecycle, promote at write time" (`SIDECAR.md`, `MCP.md`, `CHANGELOG.md`,
  `IMPLEMENTATION-STATUS.md`) — consistent.
- "No LLM summarization by design" (`ROADMAP-DECISIONS.md` #2) — no doc contradicts it.
- Beachhead = local GPU/vLLM (ADR-0004, `Prod-Intro` note, `COMPETITIVE-LANDSCAPE`) — consistent.
- `cache_epoch` mentions: **zero** across all docs; `parent_packet_id` used consistently
  (ADR-0005, `MMU-FOLD-FORK.md`, `CHANGELOG.md`).
- Packet-level fold/fork "not yet wired into the sidecar" — stated in `IMPLEMENTATION-STATUS.md`,
  `MMU-FOLD-FORK.md`, `CHANGELOG.md`, `TRACK-CD-REVIEW.md` — consistent.
- `agent_hint` "not sent anywhere / RFC open" — `AGENT-HINTS.md`, `ROADMAP-DECISIONS.md`,
  `IMPLEMENTATION-STATUS.md` — consistent.
- "Quality parity" restated as "parity on a synthetic extractive lookup/comparison set"
  (`CHANGELOG.md` findings; VERIFICATION-REPORT §D.18).

## 6. Duplicate / overlapping content (report only; nothing merged)

- `AUDIT.md` fix-status tables overlap `ENGINEERING-LOG.md` "Audit Fixes — Results" — kept
  (ledger vs narrative).
- `RERUN-REVIEW.md` N-number discussion overlaps `VERIFICATION-REPORT.md` §E — kept (both now
  bannered).
- `GAP-WAVE-MEASUREMENTS.md` re-derives `DEMO.md` / `PACKING-QUALITY.md` / `RECALL-BENCH.md`
  numbers — audit trail, kept.
- `REPO-CLEANUP.md` and this file overlap on link hygiene — kept; `REPO-CLEANUP.md` R2 items
  are annotated as resolved.
- Design docs vs `IMPLEMENTATION-STATUS.md` — now explicitly cross-linked rather than merged.

## 7. Unresolved / for the orchestrator

1. **`README.md`** was not touched (per instruction). Its remaining stale spots, before the
   rewrite: `~8.6× (44.8 vs 5.2)` (line 22) should become `8.23× (43.25 vs 5.26 req/s)`.
2. **Cited-but-untracked evidence** (raised by `REPO-CLEANUP.md` R1): the Stage-B/audit-fix
   reports referenced by `ENGINEERING-LOG.md`/`VERIFICATION-REPORT.md` (`kv_spike_20261016_142753.json`,
   `stage_b2–b5` 2026-10-16 runs, `audit_fixes*`, `audit_b6_2026-10-16*`, `stage_b7_*`) exist
   only on this machine. Decide: force-add (~13 small files) or accept docs-only evidence.
3. **Superseded artifacts on disk** for the curious reader: `packing_bench_20261017_224744.json`
   (wrong-control) and `stage_b5_20261016_153023.json` (wrong gauge, reads 0). Documented as
   historical in the bannered audits; delete or leave.
4. **no remaining `docs/adr/` references, zero broken relative links** (link check over all
   `*.md`), and no metric contradictions outstanding as of this date.

## 8. Confirmation

- `README.md`: **untouched** by this audit (no diff introduced here).
- Code: untouched. Docs only (plus this file).
- Gates re-checked after the edits: `ruff check .`, `ruff format --check .`,
  `mypy protocol evals kernel sidecar`, `pytest -q` (226) — all green.
