# DEMO-REVIEW — adversarial audit of `cfx demo` evidence (2026-10-17)

Auditor: independent verification pass (adversarial). Scope: `sidecar/demo.py`, `sidecar/render.py`,
scorer v1/v2, `tests/test_demo.py`, `tests/test_render.py`, `tests/test_scorer.py`,
`docs/engineering/DEMO.md`, and the four live receipts in `evals/reports/`. No GPU was used; no
artifact was invalidated, so **no rerun was performed** (justification in F1). No commit was made.

## Verdicts

- **Fleet claim is fair.** All six per-objective answer facts are present in **both** arms' prompts
  (reproduced offline and captured live through the real sidecar/upstream path). Both arms share one
  scorer, one model, `temperature=0.0`, and equal `max_tokens`. The only prompt asymmetry — the
  broadcast blob includes the quarantined draft while compiled packets deny it (`lifecycle:draft`) —
  is deliberate, disclosed in the receipt notes, and contains **no answer key**. The 3.39× input-token
  reduction at 6/6 vs 6/6 accuracy is exact under recomputation. Warm-cache labeling is present and
  conservative (the token claim does not depend on cache state).
- **Negative null/compliance reading is honest.** Control and treated fixtures are identical except
  for the two dead ends written through the real `POST /fabric/blocks` path (seq 27/28, ids reproduce
  byte-for-byte). All stored v1/v2 verdicts are reproduced exactly by the current scorer code from the
  saved responses (zero mismatches). The one genuine effect is exactly what the document says it is:
  the 3B treated *queue* compliance cell (0/2 v2, "SQLite-backed queue" derived from the rendered
  prohibition); the 1.5B treated v2 zeroes and the 3B control-cache v2 zeroes are correctly identified
  as v2 false negatives, not effects. No arm-biasing asymmetry found in the scorers.

## Method

Offline harness (temp, not committed; no GPU): rebuilt the fixtures, compiled/rendered all prompts,
compared against prompts captured live from the real sidecar via a fake OpenAI-compatible upstream
that logs requests; re-scored every stored response in the receipts; recomputed all totals and cache
math; ran the fleet and negative pipelines twice end-to-end and diffed request bodies/receipts.

## Findings

| ID | Severity | What | Evidence | Fixed |
|----|----------|------|----------|-------|
| F1 | S3 (fixed) | `build_fixture_store` left `observed_at`/`valid_from` as pydantic `now()` defaults — the "deterministic" fixture leaks wall-clock into stored block metadata (two builds differed in those two fields). Nothing in the compile path consumes them (`policy_engine` checks `valid_to` only; scorer has no freshness term), and blob/prompt sets were byte-identical across builds — so it is **not** content-affecting and does **not** invalidate any artifact. | Two-build field diff; timestamp-mutation test showed rendered prompts unchanged. | Yes: fixed timestamps (matches `kernel/epochs.py` convention) + `test_fixture_is_fully_deterministic_across_builds`. Content-neutral ("no GPU rerun required" verified by mutation test). |
| F2 | S3 (open, documentation) | Fleet receipt note / DEMO.md say the warm cache came from "an earlier smoke run with identical messages", but the kept smoke receipt started **2m11s after** the fleet (04:18:34Z vs 04:16:23Z); the actual warm-up run is neither preserved nor listed. The numbers themselves are consistent with a prior identical-prompt run (fleet broadcast misses: 38 ≈ 6 tails; fleet compile misses: 69 ≈ 6 tails). | Artifact `started_at` values; miss arithmetic. | No (provenance gap; recommend a run manifest in future captures). Claim is conservative, not inflated. |
| F3 | S3 (open, documentation) | "Pre-registered in DEMO.md before the rerun" cannot be independently timestamped: `DEMO.md` mtime 21:34:26 is **after** the rerun (21:30:38–21:33:05), though `demo.py` containing v2 predates it (21:29:36) and the v2 description string is baked into run-time receipt notes. The explicit post-observation admission in DEMO.md makes the overall stance honest. | File mtimes; receipt notes. | No (self-attested; the scorer code did predate the rerun). |
| F4 | S3 (open, documentation) | Claims that can only be checked against deleted receipts: "+318 compile prompt tokens over 6 requests", "completions 210 → 87", "responses are byte-identical at temperature 0" vs pre-v2 `_211536`. `evals/reports/` is git-ignored, so the deleted artifacts are unrecoverable. | `.gitignore:7`; only the 4 kept artifacts exist. | No (arithmetic plausible: the anchor is 52 o200k tokens/request × 6 = 312). Recommend archiving superseded receipts or hashes. |
| F5 | nit (open) | `tests/test_demo.py` parity test compiles with `role=spec.role`; the real server uses the default role `"user"`. The fixture has no RBAC rules, so the only difference is the `ROLE:` line; parity was re-verified with the runtime role. | `server.py` header defaults; offline render diff. | No (test remains valid; cosmetic fidelity gap). |

No S1 or S2 bugs found in demo/renderer/scorer code.

## Prompt-parity reproduction (offline + live capture)

Fleet — rendered broadcast blob (26 blocks, 6687 o200k tokens, includes the draft
`atlas_ledger_v2`) vs each objective's compiled packet (draft denied in all six):

| Objective (role) | Required | Source block | In broadcast blob | In its compiled packet |
|------------------|----------|--------------|-------------------|------------------------|
| architect | postgres, pgvector | arch | yes | yes |
| implementer | 7443 | api | yes | yes |
| reviewer | 120 | auth | yes | yes |
| tester | 9091 | staging | yes | yes |
| auditor | 820 | latency | yes | yes |
| ops | 480 | limits | yes | yes |

Live capture through the real sidecar: 12 upstream calls; the 6 broadcast system messages are
**byte-identical** to `broadcast_text(load_blocks())`; the 6 compiled system messages are
**byte-equal** to `render_packet(compile_packet(..., role="user"))` (current renderer, unchanged since
21:12:49, i.e. live when the fleet receipt was captured at 21:16:28). All 12 requests carry
`temperature=0.0` and the same `max_tokens`; compile-mode bodies are user-objective-only before
injection.

Negative composition (offline, real write path): control fabric == treated fabric pre-write (full
equality after F1); `POST /fabric/blocks` for queue+cache returns 201 / `routable` / seq 27,28 with
ids `blk_7713c856e661fc9f`, `blk_43ad7cf08bcc0814` — **exactly** the artifact write records (1.5B's
single cache write = seq 27, same id). Control prompts contain no trap dead end and end with the
fixture's unrelated prohibitions (mirror-poller, re-index); treated prompts contain the full dead
ends plus 12-word `PROHIBITIONS` lines; trap objectives are the same strings in pilot/control/treated.

## Recomputed numbers vs claims

| Claim | Artifact | Recomputed | Match |
|-------|----------|------------|-------|
| fleet prompt 44854 / 13221 (3.39×) | same | 44854 / 13221 = 3.393 | yes |
| fleet 6/6 both arms | same | re-scored all 12 responses: 12/12 same verdicts | yes |
| completions 188 / 87; wall 2.82 / 1.43 s; mean lat 470 / 238 ms | same | exact | yes |
| KV peaks 0.0605 / 0.0186 | same | exact | yes |
| warm cache 44816/44854 (99.9%), 13152/13221 (99.5%) | same | exact ratios | yes |
| smoke cold: broadcast 37280/44854 (83.1%), compile 0/13221 | same | misses 7574 = req-1 7472 + 5 warm tails (avg 20.4) — precisely "first request cold, 5 repeats hit" | yes |
| 1.5B pilot search 0/2, queue 0/2, cache 2/2, metrics 0/2, vector 0/2; frozen = cache | same | protocol replay reproduces selection | yes |
| 1.5B control 4520 tok, 2/2 v1, 2/2 v2; treated 4658 tok, 2/2 v1, 0/2 v2 | same | exact; all 14 chat/pilot responses rescore identically under both scorers | yes |
| 3B pilot queue 2/2, cache 2/2, metrics 2/2; frozen = queue, cache | same | protocol replay reproduces selection | yes |
| 3B control 4/4 v1, 2/4 v2 (2/0 by trap), 9004 tok; treated 4/4 v1, 0/4 v2, 9532 tok | same | exact; all 14 chat/pilot responses rescore identically | yes |
| 1.5B cache 4512/4520 control, 2432/4658 treated (52.2%) | same | exact | yes |
| 3B treated queue 0/2 is genuine compliance; others are v2 false negatives | response texts | confirmed by reading texts; `SQLite-backed queue` derived from the rendered prohibition | yes |

Scorer code audit: v1 is the documented mention-substring rule; v2 matches the DEMO.md mechanism
exactly (12 verbs, `set up`/`go with` bigrams, 8 exact-token negation markers, 8-token window,
`.[!?\n]` sentence split, apostrophe tokenization). Both scorers are text-only and applied to every
arm through the same code path; pilot selection uses control data only. No scorer was modified after
the final runs: pre-audit `demo.py` mtime 21:29:36 < negative receipt writes 21:31:38 / 21:33:05,
`render.py` mtime 21:12:49 < fleet receipt 21:16:28, and every stored verdict reproduces exactly from
the saved response texts. Note: the artifact receipts save full response text (good for
reproducibility) but not the prompts (reconstructed here; a future receipt could store a prompt hash).

## Flake check

Two full offline runs of each scenario against a logging fake upstream:

- request bodies (all 12 fleet; all negative pilot/chat/write) byte-identical across runs;
- receipts identical after removing `started_at`/`latency_ms`/`wall_ms`/`mean_latency_ms`;
- blob and the six compiled prompts byte-identical across two independently built fixtures;
- rendering is time-stable (identical text with `as_of` 2001 vs 2030 — no wall-clock leak into prompt
  content);
- after F1, stored fixture blocks are fully identical across builds. No RNG anywhere in `demo.py`.

## DEMO.md honesty audit

- Pre-registration subsection exists with the explicit admission that v2 was authored after observing
  v1 outputs, applied symmetrically, and not pre-registered before the first run — accurate (F3 only).
- Caveats complete: synthetic fixture; broadcast as strongest naive baseline; warm-cache labeling;
  tiny n; v2 false negatives on bullet-list/out-of-list recommendations; the single genuine compliance
  cell identified as 3B queue; the 3B probe labeled a probe, not the headline.
- Superseded artifacts listed as deleted and the four kept artifacts are the only `demo_*` files
  present (historic deletion itself is unverifiable because receipts are git-ignored — F4).
- "Cold-cache smoke" semantics are described accurately for the smoke receipt's own numbers (first
  request cold; 5 identical repeats hit; compile 0 hits); the only weakness is the unverifiable
  provenance of the fleet's warmth (F2).

## Residual risks

- n is tiny (6 fleet requests/arm; 2–4 negative requests/arm); the single real effect is a one-flip
  (2/2 vs 0/2) cell at 3B.
- Prefix-cache counters are upstream-global, vLLM-specific, and warm; the "cold" smoke depends on a
  server restart that is not recorded in any artifact.
- v1 and v2 both have documented failure modes; the compliance adjudication is a human post-hoc
  reading.
- Qwen-token claims (blob 7449; +318 anchor delta) are not reproducible offline (o200k only); the
  o200k equivalent of the anchor is 312 over 6 requests, consistent with the quoted +318.
- The fixture's written dead ends inherit real wall-clock `observed_at` from the production write
  path — correct behavior, but it means receipts should not be expected to diff bitwise at the store
  level.

## Gates (after the F1 fix and its test)

- `uv run ruff check .` — passed; `uv run ruff format --check .` — 95 files already formatted;
- `uv run mypy protocol evals kernel sidecar` — success, 41 files;
- `uv run python -m pytest -q` — **121 passed** (120 before + 1 regression test).

## Commit blockers

None. The fleet claim and the negative reading survive adversarial recomputation. F1 is a content-
neutral reproducibility fix (no artifact invalidation, no rerun needed). F2–F4 are evidence-provenance
gaps for future captures, not fairness defects; archiving superseded receipts (or their hashes) would
close them.
