# Documentation Verification Report

> **Historical snapshot (dated 2026-10-17).** This audit drove the fixes recorded in §E and
> the honest re-run; it is retained as the audit record. Where it quotes then-current
> README/CHANGELOG numbers (N3, N18, N20), prefer the current docs: the 8.6× throughput row
> was re-measured at 8.23× (43.25 vs 5.26 req/s, `stage_b5_20261017_124856.json`), and the
> test count of the time (13) is long superseded (226 today).

**Date:** 2026-10-17
**Scope:** README, CHANGELOG, `docs/engineering/*`, `docs/design/Arch-Spec.md`, ADR-0001–0005, `protocol/*`, `kernel/*`, `evals/*`, and the recorded JSON reports under `evals/reports/`.
**Method:** Read every document and experiment; traced each headline number to the producing code path; compared against the stored report files. Re-ran the offline checks only:
`pytest -q` → 13 passed; `python -m evals.spike_deterministic` → all checks pass; `evals.audit_b6.section_realrepo()` → 16.83× (64,066 → 3,806 tokens, 23 files). GPU/vLLM experiments were **not** re-run (no endpoint in this environment); their verdicts rest on the stored reports plus code inspection.

**Verdict counts (headline claims):** SUPPORTED 4 · CAVEAT-MISSING 11 · OVERSTATED 7 · UNSUPPORTED 1.

---

## A. Headline numeric claims

| # | Claim | Source | Producing experiment / file | Verdict | Note |
| :-- | :--- | :--- | :--- | :--- | :--- |
| N1 | Cross-agent cost **~2×–9×** | README:18; CHANGELOG:19 | `evals/audit_fixes.py` `section_fork_fair`; `reports/audit_fixes_20261016_154540.json` | **OVERSTATED** | 1.98× and 8.98× reproduce, but README says both are "vs best-case naive"; the 9× bound is vs `B0_top` (realistic naive), not best-case. *(README row corrected 2026-10-17.)* |
| N2 | **9.5×** role-divergent children | README:19; CHANGELOG:19; AUDIT:108 | `evals/audit_fixes2.py` `section_divergent`; `reports/audit_fixes2_20261016_160822.json` | **CAVEAT-MISSING** | 20,732.8 / 2,176 = 9.53, but B0 children get rotations of the *same* raw context (not different role slices), while CPP gets the compressed ledger; the win is mostly compression + deliberate prefix break, with no quality gate. |
| N3 | **~8.6×** throughput at 16 concurrent (44.8 vs 5.2 req/s) | README:20; CHANGES:147 | `evals/stage_b5.py`; `reports/stage_b5_20261016_153211.json` | **CAVEAT-MISSING → superseded 2026-10-17** | 44.76 / 5.19 = 8.62 reproduces, but the baseline is `B0_divergent` (no sharing). Later `audit_fixes` F6 shows `B0_tail_shared` alone reaches 35.4 req/s; CPP compression adds only 1.41×. **Fresh re-run** (`stage_b5_20261017_124856.json`): 43.25 / 5.26 = **8.23×** — the 44.8 / 5.2 / 8.6 figures are the original 2026-10-16 run and are stale as a current quote. |
| N4 | **20× lower KV** at 16 concurrent | README:21; CHANGELOG:20; AUDIT:60 | `evals/stage_b5.py`; `reports/stage_b5_20261017_124856.json` | **CAVEAT-MISSING** (re-verified 2026-10-17) | 27.95% / 1.38% = 20.2× reproduces (byte-identical across three independent runs incl. `153211`), but a finer-sampled F6 workload (`reports/audit_fixes_20261017_115748.json`) gives 13.48% / 1.16% = 11.6× vs divergent, and vs a *shared* raw prefix only 2.48% / 1.16% ≈ 2.1×. The misnamed gauge (`gpu_cache_usage_perc`) read 0 (`...153023.json`) — it did not produce the 20×. Err the headline toward ~2× vs shared, ~9–20× vs unshared. |
| N5 | **~17×** compression on real code (`httpx`) | README:22; CHANGELOG:21; AUDIT:104 | `evals/audit_b6.py` `section_realrepo`; **no stored report** | **SUPPORTED** | Reproduced offline: 64,066 → 3,806 tokens (16.83×), 23 files. Caveats: only AST signatures/classes, not semantic/body compression; uses the installed `httpx` package, so the ratio drifts with the dependency version. |
| N6 | Cold boot: **10× fewer tokens, 2× accuracy** | README:23; AUDIT:101; ENGINEERING:506 | `evals/audit_b6.py` `section_coldboot`; `reports/audit_b6_20261016_173635.json` | **OVERSTATED → superseded 2026-10-17** | Old run: 348 vs 3,566 tokens (10.25×), 100% vs 50%, but the raw arm contained **no symbol→module mapping** (not information-equivalent) and the scorer matched substrings (`module_39` counted as an answer for `symbol_3`). **Fixed + re-run 2026-10-17** (`reports/audit_b6_20261017_124755.json`): both arms carry the same mapping, exact-identifier scoring → 348 vs **3,146** tokens (**9.0×**), hologram **100%** (12/12) vs raw **25%** (3/12), stable over 3 reps. Caveat: n=4 questions × 3 reps, one 1.5B model, synthetic listing. |
| N7 | **100% quality at ~5.6× compression** | README:24; CHANGELOG:21; AUDIT:102 | `evals/audit_b6.py` `section_quality`; `reports/audit_b6_20261016_173804.json` (1.5B) and `...174056.json` (0.5B) | **CAVEAT-MISSING** | 408 / 73 = 5.59 and both accuracies are 1.0 for the lossless arm, but this is a 10-fact extractive set with 24 distractors; "compression" is noise removal, the 0.5B 20%-loss comparison is 0.75, and each question is a single sample. An earlier B6 quality run (`173635`) scored 0.0 and was rerun. |
| N8 | **15.0×** "tax" measured / substantiated | AUDIT:94; ENGINEERING:298 | `evals/audit_fixes.py` `section_claim_15x`; `reports/audit_fixes_20261016_154540.json` | **OVERSTATED** | The code computes `multi_agent_tokens = n * single_tokens`, so the ratio is identically 15.0 by construction; the model client is unused. It proves the duplication volume exists, not that CPP recovers it or that a "tax is erased". |
| N9 | **27.2×** compression from a "realistic extractor" | AUDIT:97; ENGINEERING:317 | `evals/audit_fixes.py` `section_compression`; `reports/audit_fixes_20261016_154540.json` | **CAVEAT-MISSING** | 27,431 → 1,007 reproduces, but `synth_repo()` generates highly repetitive synthetic functions; this is not real code. The real-code result is the separate 16.8× httpx run. |
| N10 | B4 **6.1× fewer tokens at 100% accuracy** | AUDIT:61; ENGINEERING:240 | `evals/stage_b4.py`; `reports/stage_b4_20261016_152935.json` | **SUPPORTED** | 572 → 93 tokens = 6.15×, accuracy 1.00 on both arms; docs already caveat that the task is trivial. Note: 12 facts, one sample per fact. |
| N11 | Intra-agent **1.65× cheaper than sliding window**; tie "fixed" | ENGINEERING:337; AUDIT:95 | `evals/audit_fixes.py` `section_intra_long`; `reports/audit_fixes_20261016_154833.json` | **CAVEAT-MISSING** | 71,251.8 / 43,263.8 = 1.647 in the cited run, but the earlier run (`...154540.json`) reported B0_window at 28,324 and **100%** retention, contradicting the cited run's 0% retention. One key probe, single sample; the favorable run is the one cited. |
| N12 | Adaptive cartridge wins at every size **4.1×–5.4×**; crossover removed | AUDIT:99; ENGINEERING:419 | `evals/audit_fixes2.py` `section_crossover`; `reports/audit_fixes2_20261016_160822.json` | **CAVEAT-MISSING** | 4.08×–5.39× reproduce, but the "adaptive" arm simply omits Tier 0 unconditionally; there is no size threshold. It compares compressed facts/pointers against raw history, and no quality check confirms the omitted cartridge is safe. |
| N13 | Fold sweep: **retention 100% at every budget** | ENGINEERING:431 | `evals/audit_fixes2.py` `section_foldsweep`; `reports/audit_fixes2_20261016_161119.json` vs `...160822.json` | **CAVEAT-MISSING** | The cited rerun says 1.0 at all budgets; the earlier run of the same code says **0.0** at all budgets. Retention is one question (`key1`), one sample per budget, and the 160822 run's cold-boot numbers also differ (649 vs 319 tokens), suggesting version/selection drift. |
| N14 | Negative knowledge **16.7% → 0%** bad-action rate | AUDIT:101; ENGINEERING:532 | `evals/audit_b6.py` `section_negknowledge`; `reports/audit_b6_20261016_173635.json` | **CAVEAT-MISSING** | 4/24 → 0/24 reproduces, but n=24 picks (3 scenarios × 4 samples), the bad option is always first, and there are no repeats or significance test. Prior round-2 probe was 0% → 0% (inconclusive). |
| N15 | FLOPs proxy added; corroborates token story | AUDIT:107; ENGINEERING:534 | `evals/audit_b6.py` `flops_proxy`; **no report** | **UNSUPPORTED** | `flops_proxy()` is defined but never called in any section; no report contains a FLOPs value, only the string note `realrepo_flops_note`. |
| N16 | Steady-state TTFT "excluded everywhere" | ENGINEERING:404; AUDIT:98 | `evals/audit_fixes2.py` `_ttft_ss` | **OVERSTATED** | Only `foldsweep` and `crossover` call `_ttft_ss`; `harness.py`, F4, F5, B4, and B6 still report mean TTFT including the cold first turn. |
| N17 | Error bars / REPEATS ± std "everywhere" | AUDIT:106,119; ENGINEERING:379 | `evals/audit_fixes.py` `REPEATS`; various reports | **OVERSTATED** | F4 and F5 use repeats; B4, B6 quality, B6 cold-boot, and B6 negative-knowledge are single-run, and several std values are exactly 0.0 because the cache made runs identical. |
| N18 | **All 27 tracked gaps fixed or accepted; none rescoped** | CHANGELOG:22; AUDIT:121 | AUDIT final table | **OVERSTATED** | AUDIT itself lists 25 FIXED, 1 OUTSTANDING (GAP-22), 1 ACCEPT (GAP-18), and GAP-08 remains open. "None rescoped" is also contradicted by GAP-02's reframing and GAP-01's identity measurement. *(CHANGELOG corrected 2026-10-17 to 25 fixed / 1 accepted / GAP-08 + GAP-22 open.)* |
| N19 | Stage B load-bearing bet **validated** on real hardware | README:13-14 | Aggregate of Stage B reports | **OVERSTATED** | The mechanism runs on vLLM, but the headline cross-agent/concurrency experiments compare a compressed one-line-fact packet to full raw observations with no quality control; cold-boot and adaptive claims have confounds; GAP-08 is undemonstrated. |
| N20 | **~1.4–2× throughput** at 16 concurrent | CHANGELOG:20 | `evals/audit_fixes.py` F6; `reports/audit_fixes_20261016_154540.json` | **OVERSTATED** | F6 shows a 1.41× *throughput increment* from compression (35.4 → 50.1 req/s) and a ~1.9× *KV* reduction, not a 1.4–2× throughput range. The number also conflicts with README's 8.6× headline. |
| N21 | Arbitrage: 0.5B compiled = 1.5B raw = 1.5B compiled = **100%** | ENGINEERING:522 | `evals/stage_b7.py`; `reports/stage_b7_Qwen_Qwen2.5-1.5B-Instruct.json` and `...-0.5B-Instruct.json` | **CAVEAT-MISSING** | Current reports give 86.7% (1.5B compiled), 93.3% (1.5B raw), 93.3% (0.5B compiled), 80% (0.5B raw), not 100%. The conclusion "undemonstrated" still holds; the quoted numbers do not match the reports. |
| N22 | B6.1 "optional halving of the number of tokens" | ENGINEERING:496 | `evals/audit_b6.py` `section_realrepo` | **UNSUPPORTED** | No code path or report implements an optional halving; this sentence has no corresponding experiment. |

---

## B. Doc/code drift

| # | Claim / spec | Source | Code reality | Verdict | Note |
| :-- | :--- | :--- | :--- | :--- | :--- |
| D1 | Tier 3 must guarantee **>85% KV-cache reuse** across turns and sub-agents | Arch-Spec:12 | `evals/stage_b2.py`; `reports/stage_b2_20261016_145513.json` | **UNSUPPORTED** | Intra-agent CPP measured 60.5% (Stage B) and 73.7% (B2); only the fork arms reach 86–92%. The normative threshold is not met on the axis the spec names. |
| D2 | Hard gates for RBAC, **purpose**, **trust-domain**, and **freshness** before scoring | Arch-Spec:11 | `kernel/router/policy_engine.py` | **UNSUPPORTED** | `apply_policy` checks lifecycle, `denied_roles`, `allowed_roles`, and `valid_to` only. `allowed_purposes`, `trust_domain`, `confidence`, and a freshness floor are never enforced. |
| D3 | Auditor role automatically strips implementer observations | Arch-Spec:74 | `kernel/router/policy_engine.py` | **UNSUPPORTED** | Stripping happens only if the block explicitly lists the role in `denied_roles`; there is no `role == "auditor" and kind == observation` rule. The test sets `denied_roles` manually. |
| D4 | Packing assertion is `total_tokens + reserve_used <= hard_total_tokens` | ADR-0005:90-91 | `kernel/router/packer.py:125-129` | **UNSUPPORTED** | The packer only compares `total` to `hard_total_tokens`; the 10% reserve is never subtracted or tracked. Packets can consume the full base. |
| D5 | 9-stage pipeline includes **MMR**/greedy packing | `kernel/router/compiler.py:4`; Arch-Spec:30 | `kernel/router/packer.py` | **UNSUPPORTED** | Packing is a deterministic utility sort + greedy fill; no MMR or diversity step exists. |
| D6 | Every compiled packet can be signed | Arch-Spec:19 | `kernel/router/compiler.py:157-165` | **SUPPORTED** | `signer` is optional; with `signer=None` the packet is emitted with `signature=None`. The claim was reworded from "MUST" to "can" to match the code. |
| D7 | Copy-on-Write sub-agent forking is validated | README:8; Arch-Spec:16; AUDIT:108 | `evals/stage_b2.py`, `audit_fixes2.py`; no fork/CoW code in `kernel/` | **UNSUPPORTED** | The experiments issue separate HTTP requests with a shared prompt prefix; no KV pages are forked or copied. The measured win is prefix-cache sharing, not CoW. |
| D8 | "No hardcoded absolutes anywhere in code" | ADR-0001:34-35 | `protocol/models.py:117-124` | **CAVEAT-MISSING** | `TokenBudget` hardcodes 600/1000/200/1800/400 as field defaults; `from_base()` exists, but the absolutes remain in code. |
| D9 | "Framework-agnostic" product | README:3 | ADR-0004:18; repo has no adapter layer | **OVERSTATED** | v1.0 scope is explicitly `raw_llm` + `vllm`/`lmcache` only; no LangGraph/CrewAI/AutoGen/MCP/A2A adapters exist. |
| D10 | B6.4 arbitrage 100% figures | ENGINEERING:522 | `stage_b7_*.json` | **CAVEAT-MISSING** | See N21; the current reports disagree with the doc. |
| D11 | RFC 8785 canonicalization | README:44; CHANGELOG:9 | `protocol/canonical.py` | **SUPPORTED** | Self-check passes the RFC 8785 vectors used for packet values; the module documents that it is a subset (full exponent edge cases are not implemented). |
| D12 | Immutable schemas, unknown fields rejected | CHANGELOG:9; ADR-0005:93 | `protocol/models.py` | **SUPPORTED** | All models use `frozen=True, extra="forbid"`; `pytest` passes. |
| D13 | Tier budgets 15/25/5/45/10 and 4,000 base | Arch-Spec:10; ADR-0001 | `protocol/models.py:117-145`; `test_kernel.py` | **SUPPORTED** | `from_base(4000)` yields 600/1000/200/1800/400 and the validator enforces the sum. |
| D14 | Tier 1 is append-only and prefix-stable | ADR-0002; Arch-Spec:12 | `spike_deterministic.py`; `test_kernel.py` | **SUPPORTED** | Deterministic spike asserts prefix stability across watermarks; tests pass. |
| D15 | Compiler is deterministic and content-hashed | ADR-0003; ADR-0005 | `kernel/router/compiler.py`; `test_kernel.py` | **SUPPORTED** | Two compiles produce the same `content_hash`; `packet_content_hash` empties the hashes block as specified. |

---

## C. Cross-cutting problems behind the table

1. **The fork/crossover/concurrency arms are not information-equivalent comparisons.** `stage_b2.cpp_intra_builder`, `stage_b2.cpp_child`, `audit_fixes.section_fork_fair`, and `audit_fixes2.section_*` give CPP a cartridge + one-line `facts()` + `pointers()`, while B0 receives the full multi-line raw observations. The measured token win is therefore partly (or mostly) **loss of information**, not compression of the same information. No experiment checks that a child given the CPP packet can answer the same questions as a child given the raw transcript. This affects N1, N2, N3, N4, N11, N12, and the "V2 compression" story.
2. **Only the B4/B6 quality tests check correctness, and they use a different pipeline** (`stage_b4.py`, `audit_b6.section_quality`) than the fork/concurrency cost tests. The cross-agent cost claims are never paired with a quality parity check.
3. **Several "fixed" gaps are fixed only in a synthetic probe, not in the product path.** Adaptive cartridge (N12) is an unconditional no-cartridge arm; FLOPs (N15) is an unused function; cold boot (N6) is a confounded prompt comparison; negative knowledge (N14) is 24 picks with an always-first bad option.
4. **Selective reporting.** There are two `audit_fixes` reports with contradictory intra-long results (154540 vs 154833) and two `audit_fixes2` reports with contradictory fold-retention results (160822 vs 161119). The docs cite the version that supports the claim. There is also an early B6 quality run (`173635`) with all-zero accuracy that was rerun (`173804`); the docs cite only the rerun.
5. **Internal inconsistency between headline docs.** README says 8.6× throughput and 20× KV; CHANGELOG says 1.4–2× throughput and 20× KV. README said 2–9× "vs best-case" (corrected 2026-10-17); CHANGELOG correctly says 2× best-case, 9× realistic. AUDIT says 25 FIXED + 1 OUTSTANDING + 1 ACCEPT; CHANGELOG said all 27 fixed or accepted (corrected 2026-10-17).
6. **The 20× KV number is not reproducible across the two fixed-gauge runs.** `stage_b5_153211` had B0 at 27.95% (with 13 waiting); `audit_fixes` F6 had B0 at 10.1% (also 13 waiting). The later, 25 ms-sampled run gives 8.7× vs divergent and ~1.9× vs shared raw. The 20× headline is the most fragile number in the README. *(Update 2026-10-17: the 20.2× reproduces byte-identically under `stage_b5`'s 100 ms sampler — fresh `124856` matches `153211` and `115911`; the lower F6 numbers are a different workload/sampling, and the wrong gauge name in `153023` read 0, not 20×. Defensible form: ~20× vs unshared raw, ~2× vs shared raw.)*
7. **CoW is a narrative label, not an implementation.** No code in `kernel/` forks packets or KV state; the evals reuse a prompt prefix. The README's "Copy-on-Write sub-agent forking" and the 9.5× "naive can't share" story should be described as prefix-cache sharing until a real CoW path exists.

---

## D. Prioritized documentation fixes

### P0 — must fix before repeating these numbers externally

1. **Withdraw or relabel the cold-boot claim (N6).** Either add the symbol→module mapping to the raw arm and re-run, or state that the raw arm was unanswerable by construction. The current "2× accuracy" is a comparison between a context that contains the answer and one that does not.
2. **Fix the README 2–9× basis (N1).** Say "~2× vs best-case naive (`B0_tail`), ~9× vs realistic naive (`B0_top`)" exactly as CHANGELOG does. Add: the CPP arm carries a one-line-fact packet, not the raw transcript.
3. **Reconcile throughput/KV headlines (N3, N4, N20).** State the baseline for 8.6× (`B0_divergent`), note that sharing alone reaches ~35 req/s and compression adds ~1.4×, and replace the 20× KV headline with the reproducible range (≈8.7× vs divergent, ≈1.9× vs shared raw) or re-run to confirm 20×.
4. **Downgrade the 15× claim (N8)** from "measured/substantiated" to "arithmetic duplication factor; not a measured recovery". Remove it from the "fixed" column unless a model-backed experiment is added.
5. **Correct "all 27 gaps fixed" (N18)** to the AUDIT's own tally: 25 FIXED, 1 OUTSTANDING (GAP-22 harder quality), 1 ACCEPT (GAP-18), GAP-08 open. Remove "none rescoped" or explain the GAP-01/GAP-02 reframings.
6. **Scope "error bars" and "steady-state TTFT" (N16, N17)** to the sections that actually use them. Mark B4/B6 quality, cold boot, and negative knowledge as single-sample.
7. **Mark 27.2× as synthetic-only (N9)** and 16.8× as real-code signature extraction (N5); add a stored report for the realrepo run or state it is reproducible only from the current `httpx` install.
8. **Add unequal-information caveats to 9.5×, 4.1–5.4×, and 1.65× (N2, N11, N12).** State that CPP sees `facts()`/`pointers()` while B0 sees raw observations, and that no quality parity check was run on those arms.

### P1 — correctness of the record

9. **Fix the FLOPs claim (N15)**: either call `flops_proxy()` in a report or mark GAP-16 "not measured".
10. **Fix the B6.4 arbitrage numbers (N21)** to 86.7/93.3/93.3/80 (or re-run with a harder task); keep the "undemonstrated" conclusion.
11. **Re-run or explain the discordant reports**: intra-long 154540 vs 154833; fold retention 160822 vs 161119; B6 quality 173635 vs 173804. Document which run is canonical and why.
12. **Fix the stage_b5 report filename typo** (`20260316` → `20261016`) and add `kernel/` to the README repository layout.
13. **Resolve the spec/code drift** in D1–D7: either implement the policy gates, auditor auto-redaction, reserve enforcement, MMR, mandatory signing, and CoW fork, or label them explicitly as post-v1.0/spec-only. In particular, the `>85%` invariant should be restated as an empirical target with the measured intra-agent numbers.
14. **Replace "framework-agnostic"** with the ADR-0004 scope (`raw_llm` + `vllm`/`lmcache` for v1.0) until adapters exist.

### P2 — polish

15. Add sample sizes and repeat counts to every numeric table (B4 n=12 questions; B6 quality n=10 facts + 4 comparisons; B6 negative n=24 picks; cold-boot n=4 questions).
16. Qualify the httpx ratio as dependency-version-dependent and pin the package version in the report.
17. Remove the unsupported "optional halving of tokens" sentence (N22).
18. Restate "quality parity" as "parity on a synthetic extractive lookup/comparison set" rather than general task quality, and keep GAP-22 OUTSTANDING visible next to it.

---

## E. Superseding runs (2026-10-17)

N6 and N4 were fixed and re-run on the local GPU (vLLM 0.31.0, RTX 5070 Laptop 8 GB, ~2.5 min total GPU wall-clock):

- **Cold boot:** raw arm made information-equal + exact-identifier scoring; `evals/reports/audit_b6_20261017_124755.json` → 348 vs 3,146 tok (**9.0×**), hologram **100%** vs raw **25%** (12/12 vs 3/12, 3 reps).
- **KV:** `evals/reports/stage_b5_20261017_124856.json` → 27.95% vs 1.38% = **20.2× vs `B0_divergent`** (reproduces `153211`); verified F6 `evals/reports/audit_fixes_20261017_115748.json` → 11.6× vs divergent but only **2.1× vs `B0_tail_shared`** (shared raw prefix).

Details and supersede pointers: `docs/engineering/ENGINEERING-LOG.md`, section "Honest re-run".
