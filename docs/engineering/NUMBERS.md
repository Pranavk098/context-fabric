# Canonical numbers registry

Headline figures in this repo had a real drift bug (README carried a superseded
throughput number that only a manual audit caught). This file is the single registry of
the canonical set. The fenced `json` block at the bottom is machine-readable and is read
by `scripts/check_docs_numbers.py` (wired into CI). **To change a headline number, edit
the docs and this registry in the same commit.**

Legend

- **strict** — derived from the listed artifact under `evals/reports/`; the checker
  verifies the artifact exists and (where an expression is given) recomputes the value
  from the artifact's own fields. If the artifact moves, CI fails until docs + registry move.
- **display-only** — no machine artifact exists (or the number is not mechanically
  derivable); the checker only asserts the display string still appears in the listed docs.
  A human re-verifies these when they change.
- **baseline** — every `strict` entry names the arm its value is measured against, and it is
  the *strongest* naive arm, not a strawman. `scripts/check_docs_numbers.py` fails a strict
  entry that omits it, so a headline cannot ship without its comparison (see the JSON
  `baseline` field per entry).

| id | Value as displayed | Type | Where it must appear | Artifact (source of truth) | How verified |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `pilot_prompt_token_ratio` | `35.87x` | strict | `README.md`, `docs/engineering/PILOT.md` | `evals/reports/pilot_20261018_013151.json` | 193366 / 5391 = 35.8683 → 35.87 (real httpx, 32k ctx) |
| `demo_prompt_token_ratio` | `3.17x` | strict | `README.md`, `docs/engineering/DEMO.md`, `CHANGELOG.md` | `evals/reports/demo_fleet_20261017_231929.json` | 44854 / 14170 = 3.1654 → 3.17 |
| `demo_tokens_saved` | `30,684` | strict | `README.md` | same demo receipt | 44854 − 14170 = 30684 |
| `demo_broadcast_tokens` | `44,854` | strict | `README.md` | same demo receipt | `arms.broadcast.prompt_tokens_total` |
| `demo_compile_tokens` | `14,170` | strict | `README.md` | same demo receipt | `arms.compile.prompt_tokens_total` |
| `demo_accuracy_parity` | `6/6` | strict | `README.md`, `docs/engineering/DEMO.md` | same demo receipt | broadcast `passed`/`total` = 1.0; compile also 6/6 |
| `kv_peak_ratio` | `20.2x` | strict | `README.md`, `CHANGELOG.md` | `evals/reports/stage_b5_20261017_124856.json` | 0.279534 / 0.013823 = 20.22 → 20.2 (k=16, unshared raw vs CPP) |
| `kv_peak_percentages` | `27.95% vs 1.38%` | strict | `CHANGELOG.md`, `docs/engineering/RERUN-REVIEW.md` | same stage_b5 receipt | raw `peak_kv_cache_usage_perc` values; ratio 20.22 as above |
| `throughput_ratio` | `8.23x` | strict | `README.md`, `CHANGELOG.md` | same stage_b5 receipt | 43.2502 / 5.2575 = 8.2264 → 8.23 (k=16) |
| `throughput_detail` | `43.25 vs 5.26 req/s` | strict | `README.md`, `CHANGELOG.md` | same stage_b5 receipt | raw k=16 throughputs (CPP vs `B0_divergent`) |
| `coldboot_tokens` | `348 vs 3,146` | strict | `README.md`, `CHANGELOG.md` | `evals/reports/audit_b6_20261017_124755.json` | token fields of the information-equal re-run (n=4×3) |
| `coldboot_ratio` | `9.0x` | strict | `CHANGELOG.md` | same cold-boot receipt | 3146 / 348 = 9.0402 → 9.0 |
| `coldboot_accuracy` | `100% vs 25%` | strict | `README.md`, `CHANGELOG.md` | same cold-boot receipt | hologram 1.0 vs raw 0.25 (3 reps) |
| `recall_top1_delta` | `0.00 → 0.85` | strict | `README.md`, `docs/engineering/RECALL-BENCH.md` | `evals/reports/recall_bench_20261018T061752Z.json` | BM25 top1 0.85 − lexical top1 0.00 = 0.85 |
| `recall_top1_delta_compact` | `0.00→0.85` | strict | `CHANGELOG.md` | same recall receipt | same delta (CHANGELOG renders it without spaces) |
| `packing_redundancy` | `-13.4%` | strict | `README.md`, `CHANGELOG.md` | `evals/reports/packing_bench_20261017_231759.json` | (0.205616 − 0.177986) = 0.0276 jaccard delta; /0.205616 = 13.4% |
| `quality_parity_ratio` | `~5.6x` | strict | `README.md`, `CHANGELOG.md` | `evals/reports/audit_b6_20261016_173804.json` | 408 / 73 = 5.589 → 5.6 (1.5B, lossless arm; N7 caveats) |
| `httpx_compression` | `~17x` | display-only | `README.md`, `CHANGELOG.md` | — (N5: no stored report) | 64,066 → 3,806 tokens; drifts with the installed `httpx` version |
| `test_count` | `338` | display-only | `README.md` | — | `uv run python -m pytest --collect-only -q` on the current tree (3 embedding/symlink tests skip without the extra/on Windows); recount and update README + this row together |

Stale patterns (must never appear as current numbers in their scoped docs; historical
docs that explicitly quote superseded runs are exempt via `historical_ok`):

| pattern | scope | why |
| :--- | :--- | :--- |
| `8.6x` | `README.md`, `docs/engineering/DEMO.md`, `CHANGELOG.md` | original 2026-10-16 throughput run, superseded by 8.23x |
| `44.8` | same | same superseded run (baseline 44.8 req/s) |
| `5.2 req/s` | same | same superseded run (baseline 5.2 req/s) |

```json
{
  "entries": [
    {
      "id": "pilot_prompt_token_ratio",
      "claim": "real-repo pilot (pre-registered): compile vs raw prompt-token reduction",
      "baseline": "raw arm: the full 32,259-token httpx bundle as one system message (strongest naive), same model",
      "display": "35.87x",
      "kind": "strict",
      "artifact": "evals/reports/pilot_20261018_013151.json",
      "recompute": {
        "expr": "a/b",
        "a": "arms.raw.prompt_tokens_total",
        "b": "arms.compile.prompt_tokens_total",
        "expect": 35.87,
        "round": 2
      },
      "must_appear_in": ["README.md", "docs/engineering/PILOT.md"]
    },
    {
      "id": "pilot_raw_tokens",
      "claim": "real-repo pilot: raw arm prompt tokens",
      "baseline": "raw arm: full 32,259-token httpx bundle as one system message",
      "display": "193,366",
      "kind": "strict",
      "artifact": "evals/reports/pilot_20261018_013151.json",
      "must_appear_in": ["docs/engineering/PILOT.md"]
    },
    {
      "id": "pilot_compile_tokens",
      "claim": "real-repo pilot: compile arm prompt tokens",
      "baseline": "raw arm: full 32,259-token httpx bundle as one system message",
      "display": "5,391",
      "kind": "strict",
      "artifact": "evals/reports/pilot_20261018_013151.json",
      "must_appear_in": ["docs/engineering/PILOT.md"]
    },
    {
      "id": "demo_prompt_token_ratio",
      "claim": "cfx demo fleet: compile vs broadcast prompt-token reduction",
      "baseline": "broadcast arm: full raw state to every agent (strongest naive, prefix-cache friendly)",
      "display": "3.17x",
      "kind": "strict",
      "artifact": "evals/reports/demo_fleet_20261017_231929.json",
      "recompute": {
        "expr": "a/b",
        "a": "arms.broadcast.prompt_tokens_total",
        "b": "arms.compile.prompt_tokens_total",
        "expect": 3.17,
        "round": 2
      },
      "must_appear_in": ["README.md", "docs/engineering/DEMO.md", "CHANGELOG.md"]
    },
    {
      "id": "demo_tokens_saved",
      "claim": "cfx demo fleet: prompt tokens saved across the 6 requests",
      "baseline": "broadcast arm: full raw state to every agent (strongest naive)",
      "display": "30,684",
      "kind": "strict",
      "artifact": "evals/reports/demo_fleet_20261017_231929.json",
      "recompute": {
        "expr": "a-b",
        "a": "arms.broadcast.prompt_tokens_total",
        "b": "arms.compile.prompt_tokens_total",
        "expect": 30684,
        "round": 0
      },
      "must_appear_in": ["README.md"]
    },
    {
      "id": "demo_broadcast_tokens",
      "claim": "cfx demo fleet: broadcast arm prompt tokens",
      "baseline": "broadcast arm itself: full raw state to every agent",
      "display": "44,854",
      "kind": "strict",
      "artifact": "evals/reports/demo_fleet_20261017_231929.json",
      "must_appear_in": ["README.md"]
    },
    {
      "id": "demo_compile_tokens",
      "claim": "cfx demo fleet: compile arm prompt tokens",
      "baseline": "broadcast arm: full raw state to every agent (strongest naive)",
      "display": "14,170",
      "kind": "strict",
      "artifact": "evals/reports/demo_fleet_20261017_231929.json",
      "must_appear_in": ["README.md"]
    },
    {
      "id": "demo_accuracy_parity",
      "claim": "cfx demo fleet: identical accuracy both arms",
      "baseline": "broadcast arm: full raw state to every agent (strongest naive)",
      "display": "6/6",
      "kind": "strict",
      "artifact": "evals/reports/demo_fleet_20261017_231929.json",
      "recompute": {
        "expr": "a/b",
        "a": "arms.broadcast.passed",
        "b": "arms.broadcast.total",
        "expect": 1.0,
        "round": 2
      },
      "must_appear_in": ["README.md", "docs/engineering/DEMO.md"]
    },
    {
      "id": "kv_peak_ratio",
      "claim": "16 concurrent agents: KV peak ratio, unshared raw (B0_divergent) vs CPP shared",
      "baseline": "B0_divergent (unshared raw broadcast, k=16); vs a shared raw prefix the win is only ~2x",
      "display": "20.2x",
      "kind": "strict",
      "artifact": "evals/reports/stage_b5_20261017_124856.json",
      "recompute": {
        "expr": "a/b",
        "a": "cases.2.peak_kv_cache_usage_perc",
        "b": "cases.5.peak_kv_cache_usage_perc",
        "expect": 20.2,
        "round": 1
      },
      "must_appear_in": ["README.md", "CHANGELOG.md"]
    },
    {
      "id": "kv_peak_percentages",
      "claim": "16 concurrent agents: KV peak percentages (unshared raw vs CPP shared)",
      "baseline": "B0_divergent (unshared raw broadcast, k=16); ~2x vs a shared raw prefix",
      "display": "27.95% vs 1.38%",
      "kind": "strict",
      "artifact": "evals/reports/stage_b5_20261017_124856.json",
      "recompute": {
        "expr": "a/b",
        "a": "cases.2.peak_kv_cache_usage_perc",
        "b": "cases.5.peak_kv_cache_usage_perc",
        "expect": 20.22,
        "round": 2
      },
      "must_appear_in": ["CHANGELOG.md", "docs/engineering/RERUN-REVIEW.md"]
    },
    {
      "id": "throughput_ratio",
      "claim": "16 concurrent agents: throughput ratio, CPP shared vs unshared raw",
      "baseline": "B0_divergent (unshared raw broadcast, k=16); vs a shared raw prefix the win is ~2x",
      "display": "8.23x",
      "kind": "strict",
      "artifact": "evals/reports/stage_b5_20261017_124856.json",
      "recompute": {
        "expr": "a/b",
        "a": "cases.5.throughput_req_per_s",
        "b": "cases.2.throughput_req_per_s",
        "expect": 8.23,
        "round": 2
      },
      "must_appear_in": ["README.md", "CHANGELOG.md"]
    },
    {
      "id": "throughput_detail",
      "claim": "16 concurrent agents: raw throughputs at k=16",
      "baseline": "B0_divergent (unshared raw broadcast); shared raw prefix alone reaches ~35 req/s",
      "display": "43.25 vs 5.26 req/s",
      "kind": "strict",
      "artifact": "evals/reports/stage_b5_20261017_124856.json",
      "must_appear_in": ["README.md", "CHANGELOG.md"]
    },
    {
      "id": "coldboot_tokens",
      "claim": "cold boot (information-equal re-run): hologram vs raw file tokens",
      "baseline": "raw arm: information-equal full file listing, same model",
      "display": "348 vs 3,146",
      "kind": "strict",
      "artifact": "evals/reports/audit_b6_20261017_124755.json",
      "recompute": {
        "expr": "a/b",
        "a": "coldboot.raw_tokens",
        "b": "coldboot.hologram_tokens",
        "expect": 9.0402,
        "round": 4
      },
      "must_appear_in": ["README.md", "CHANGELOG.md"]
    },
    {
      "id": "coldboot_ratio",
      "claim": "cold boot (information-equal re-run): token ratio raw / hologram",
      "baseline": "raw arm: information-equal full file listing, same model",
      "display": "9.0x",
      "kind": "strict",
      "artifact": "evals/reports/audit_b6_20261017_124755.json",
      "recompute": {
        "expr": "a/b",
        "a": "coldboot.raw_tokens",
        "b": "coldboot.hologram_tokens",
        "expect": 9.0,
        "round": 1
      },
      "must_appear_in": ["CHANGELOG.md"]
    },
    {
      "id": "coldboot_accuracy",
      "claim": "cold boot (information-equal re-run): accuracy ratio hologram / raw",
      "baseline": "raw arm: information-equal full file listing, same model",
      "display": "100% vs 25%",
      "kind": "strict",
      "artifact": "evals/reports/audit_b6_20261017_124755.json",
      "recompute": {
        "expr": "a/b",
        "a": "coldboot.hologram_accuracy",
        "b": "coldboot.raw_accuracy",
        "expect": 4.0,
        "round": 1
      },
      "must_appear_in": ["README.md", "CHANGELOG.md"]
    },
    {
      "id": "recall_top1_delta",
      "claim": "recall bench: BM25 top-1 minus old lexical top-1",
      "baseline": "old lexical recall baseline (pre-BM25)",
      "display": "0.00 → 0.85",
      "kind": "strict",
      "artifact": "evals/reports/recall_bench_20261018T061752Z.json",
      "recompute": {
        "expr": "a-b",
        "a": "aggregates.bm25.top1",
        "b": "aggregates.lexical.top1",
        "expect": 0.85,
        "round": 2
      },
      "must_appear_in": ["README.md", "docs/engineering/RECALL-BENCH.md"]
    },
    {
      "id": "recall_top1_delta_compact",
      "claim": "recall bench: same top-1 delta, CHANGELOG renders it without spaces",
      "baseline": "old lexical recall baseline (pre-BM25)",
      "display": "0.00→0.85",
      "kind": "strict",
      "artifact": "evals/reports/recall_bench_20261018T061752Z.json",
      "recompute": {
        "expr": "a-b",
        "a": "aggregates.bm25.top1",
        "b": "aggregates.lexical.top1",
        "expect": 0.85,
        "round": 2
      },
      "must_appear_in": ["CHANGELOG.md"]
    },
    {
      "id": "packing_redundancy",
      "claim": "lambda-MMR packing: average pairwise Jaccard reduction vs greedy",
      "baseline": "pre-MMR greedy packer (mmr_lambda=1.0), same budget; synthetic fixture",
      "display": "-13.4%",
      "kind": "strict",
      "artifact": "evals/reports/packing_bench_20261017_231759.json",
      "recompute": {
        "expr": "a-b",
        "a": "arms.greedy.avg_pairwise_jaccard",
        "b": "arms.mmr.avg_pairwise_jaccard",
        "expect": 0.0276,
        "round": 4
      },
      "must_appear_in": ["README.md", "CHANGELOG.md"]
    },
    {
      "id": "quality_parity_ratio",
      "claim": "B6 quality: raw-full / lossless-compile token ratio at 100% accuracy",
      "baseline": "raw-full context arm (lossless compile), same model",
      "display": "~5.6x",
      "kind": "strict",
      "artifact": "evals/reports/audit_b6_20261016_173804.json",
      "recompute": {
        "expr": "a/b",
        "a": "quality.raw_full.tokens",
        "b": "quality.compiled_lossy_0.tokens",
        "expect": 5.6,
        "round": 1
      },
      "must_appear_in": ["README.md", "CHANGELOG.md"]
    },
    {
      "id": "cost_demo_gpt4o_warm_native",
      "claim": "B2 cost model: demo fleet native-cache-only cost, openai:gpt-4o, warm cache",
      "baseline": "sidecar+cache cost for the same workload ($0.0388)",
      "display": "$0.0639",
      "kind": "display-only",
      "must_appear_in": ["docs/engineering/COSTS.md"]
    },
    {
      "id": "cost_demo_gpt4o_warm_sidecar",
      "claim": "B2 cost model: demo fleet sidecar+cache cost, openai:gpt-4o, warm cache",
      "baseline": "native-cache-only cost for the same workload ($0.0639)",
      "display": "$0.0388",
      "kind": "display-only",
      "must_appear_in": ["docs/engineering/COSTS.md"]
    },
    {
      "id": "cost_demo_gpt4o_miss_native",
      "claim": "B2 cost model: demo fleet native-cache-only cost, openai:gpt-4o, TTL miss",
      "baseline": "sidecar+cache cost for the same workload ($0.0388)",
      "display": "$0.1140",
      "kind": "display-only",
      "must_appear_in": ["docs/engineering/COSTS.md"]
    },
    {
      "id": "cost_demo_gpt4o_miss_sidecar",
      "claim": "B2 cost model: demo fleet sidecar+cache cost, openai:gpt-4o, TTL miss",
      "baseline": "native-cache-only cost for the same workload ($0.1140)",
      "display": "$0.0388",
      "kind": "display-only",
      "must_appear_in": ["docs/engineering/COSTS.md"]
    },
    {
      "id": "cost_demo_sonnet_warm_native",
      "claim": "B2 cost model: demo fleet native-cache-only cost, anthropic:sonnet, warm cache",
      "baseline": "sidecar+cache cost for the same workload ($0.0468)",
      "display": "$0.0541",
      "kind": "display-only",
      "must_appear_in": ["docs/engineering/COSTS.md"]
    },
    {
      "id": "cost_demo_sonnet_warm_sidecar",
      "claim": "B2 cost model: demo fleet sidecar+cache cost, anthropic:sonnet, warm cache",
      "baseline": "native-cache-only cost for the same workload ($0.0541)",
      "display": "$0.0468",
      "kind": "display-only",
      "must_appear_in": ["docs/engineering/COSTS.md"]
    },
    {
      "id": "cost_demo_sonnet_miss_native",
      "claim": "B2 cost model: demo fleet native-cache-only cost, anthropic:sonnet, TTL miss",
      "baseline": "sidecar+cache cost for the same workload ($0.0468)",
      "display": "$0.2878",
      "kind": "display-only",
      "must_appear_in": ["docs/engineering/COSTS.md"]
    },
    {
      "id": "cost_demo_sonnet_miss_sidecar",
      "claim": "B2 cost model: demo fleet sidecar+cache cost, anthropic:sonnet, TTL miss",
      "baseline": "native-cache-only cost for the same workload ($0.2878)",
      "display": "$0.0468",
      "kind": "display-only",
      "must_appear_in": ["docs/engineering/COSTS.md"]
    },
    {
      "id": "cost_pilot_gpt4o_warm_native",
      "claim": "B2 cost model: httpx pilot native-cache-only cost, openai:gpt-4o, warm cache",
      "baseline": "sidecar+cache cost for the same workload ($0.0157)",
      "display": "$0.2420",
      "kind": "display-only",
      "must_appear_in": ["docs/engineering/COSTS.md"]
    },
    {
      "id": "cost_pilot_gpt4o_warm_sidecar",
      "claim": "B2 cost model: httpx pilot sidecar+cache cost, openai:gpt-4o, warm cache",
      "baseline": "native-cache-only cost for the same workload ($0.2420)",
      "display": "$0.0157",
      "kind": "display-only",
      "must_appear_in": ["docs/engineering/COSTS.md"]
    },
    {
      "id": "cost_pilot_sonnet_warm_native",
      "claim": "B2 cost model: httpx pilot native-cache-only cost, anthropic:sonnet, warm cache",
      "baseline": "sidecar+cache cost for the same workload ($0.0187)",
      "display": "$0.1794",
      "kind": "display-only",
      "must_appear_in": ["docs/engineering/COSTS.md"]
    },
    {
      "id": "cost_pilot_sonnet_warm_sidecar",
      "claim": "B2 cost model: httpx pilot sidecar+cache cost, anthropic:sonnet, warm cache",
      "baseline": "native-cache-only cost for the same workload ($0.1794)",
      "display": "$0.0187",
      "kind": "display-only",
      "must_appear_in": ["docs/engineering/COSTS.md"]
    },
    {
      "id": "httpx_compression",
      "claim": "real-code compression on httpx (N5: no stored report)",
      "baseline": "raw httpx source (no stored report - re-verify by hand)",
      "display": "~17x",
      "kind": "display-only",
      "must_appear_in": ["README.md", "CHANGELOG.md"]
    },
    {
      "id": "test_count",
      "claim": "test-suite size badge (current tree; recount with pytest when tests change)",
      "display": "338",
      "kind": "display-only",
      "must_appear_in": ["README.md"]
    }
  ],
  "stale_patterns": [
    {
      "pattern": "8.6x",
      "docs": ["README.md", "docs/engineering/DEMO.md", "CHANGELOG.md"],
      "why": "original 2026-10-16 throughput run; superseded by 8.23x"
    },
    {
      "pattern": "44.8",
      "docs": ["README.md", "docs/engineering/DEMO.md", "CHANGELOG.md"],
      "why": "same superseded run (baseline 44.8 req/s)"
    },
    {
      "pattern": "5.2 req/s",
      "docs": ["README.md", "docs/engineering/DEMO.md", "CHANGELOG.md"],
      "why": "same superseded run (baseline 5.2 req/s)"
    }
  ],
  "historical_ok": [
    "docs/engineering/AUDIT.md",
    "docs/engineering/CHANGES-FROM-PROPOSAL.md",
    "docs/engineering/DEMO-REVIEW.md",
    "docs/engineering/DOCS-AUDIT.md",
    "docs/engineering/ENGINEERING-LOG.md",
    "docs/engineering/RERUN-REVIEW.md",
    "docs/engineering/VERIFICATION-REPORT.md"
  ]
}
```
