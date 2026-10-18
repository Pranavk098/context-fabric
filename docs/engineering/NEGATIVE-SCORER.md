# Negative-knowledge scorers — v1/v2 failure taxonomy and v3 pre-registration

This document is the pre-registration of **scorer v3** (`sidecar/negscore.py` — written for
`evals/negscore.py` on **2026-10-18, before any v3 code was written**, and moved to the
sidecar on 2026-10-18 with **no rule changes** when all three scorers were consolidated;
`evals/negscore.py` is now a re-export shim). It records (a) the historical failure modes of
v1 and v2 with concrete examples, (b) the v3 rule set exactly as it will be implemented, and
(c) the anti-tuning statement. The offline comparison (§3, historical), the fresh GPU rerun
comparison (§5, **canonical**) and the limitations section (§4) are appended after the run;
the rule set text above them was frozen before the code existed.

Scope: this is a **scorer** for the negative-knowledge demo protocol (`cfx demo --scenario
negative`) — it decides whether a stored response recommends a trap's bad tool. It says
nothing about answer quality, and it is not a judge.

## 1. Historical failure taxonomy (v1, v2)

Sources: `DEMO.md` (§Scorer v2, §Results), `GAP-WAVE-MEASUREMENTS.md` (manual-adjudication
rows), and the stored artifacts `evals/reports/demo_negative_20261017_231935.json` /
`demo_negative_3b_20261017_232032.json` (both deleted in the 2026-10-18 supersession; still
recoverable via `git show HEAD:<path>`).

| # | scorer | failure mode | example | observed |
|---|--------|--------------|---------|----------|
| F1 | v1 | **False positive: negation/compliance mention.** Any substring match counts, so an answer that cites the prohibition and complies scores bad. | "Given the prohibitions against Redis and Celery... use the SQLite-backed queue." | 1.5B treated cache response 0 (earlier set; `DEMO.md`) |
| F2 | v1 | **False positive: mere mention.** A tool named only as the failed past approach scores bad. | "...avoids the issues associated with Redis, which had a significant data loss during a failover event." | 3B treated queue (both repeats; `DEMO.md`) |
| F3 | v2 | **False negative: heading/bullet-initial mention.** Tool leads a heading or bullet with no listed recommendation verb before it. | "**Redis**: A popular choice for in-memory caching..." | 1.5B treated cache response 1 (`_231935`); GAP-WAVE quotes "**Redis Cache**" |
| F4 | v2 | **False negative: bullet-list recommendations.** Numbered bullets lead with the tool; the marker is stripped by sentence splitting. | "1. **Redis Cache**: ..." (pilot), "2. **Redis** | - **Pros**: ..." (3B pilot/control queue) | `_231935`, `_232032` |
| F5 | v2 | **False negative: modal phrasing.** "could be X" carries the recommendation; no listed verb precedes the mention. | "A distributed cache could be Redis or a similar system." | documented class; crafted repro below |
| F6 | v2 | **False positive: fixed 8-token negation window.** The cue (`instead`) sits just outside the window, so a contrast/alternative mention scores bad. | "...using the store's built-in row cache instead of adding another caching layer like Redis or Memcached." → v2 flags `memcached` | `DEMO.md` caveats; GAP-WAVE manual adjudication (earlier set) |
| F7 | v2 | **False negative: unlisted tool.** RabbitMQ (control 3B queue) is not in the trap's keyword list at all — a keyword-set limitation, not a rule bug. | control queue answers recommend RabbitMQ, never listed | `DEMO.md` 3B control queue |

v2's other known limits (stated in `DEMO.md`): passive constructions ("Redis is
recommended") and recommendation verbs outside the listed set are not caught; the marker
set is exact-token; the window is fixed.

## 2. Scorer v3 — pre-registered rule set (2026-10-18)

Fixed from the taxonomy above. Constants (cue/verb lists) live in `sidecar/negscore.py`
(moved there 2026-10-18; `evals/negscore.py` re-exports them), matching this section; the
test suite (`tests/test_negscore.py`) asserts the historical cases (F1–F6) flip correctly.

### 2.1 Segmentation

- Split the response into **segments**: one segment per sentence (split on `.` `!` `?`) and
  one per line (split on `\n`). Markdown bullet/heading lines (`-`, `*`, `+`, `•`, `>`,
  `#`, or `N.`-style ordered markers, possibly stacked like `### 1.`) are kept as bullet
  context: the leading marker is stripped, the rest is sentence-split, and each resulting
  segment is tagged as bullet-initial context. This keeps bullets as separate segments and
  preserves the fact that the tool led the bullet (F3, F4).

### 2.2 Bad-tool mention

- Case-insensitive, word-boundary match of each bad term; multi-word terms (e.g.
  `apache kafka`) match on consecutive word tokens.

### 2.3 RECOMMENDATION

A segment carrying a bad-tool mention is a RECOMMENDATION when any of:

1. **Verb act**: a listed recommendation verb appears **before the mention in the same
   clause** (the tool is the object). Verbs: `recommend`, `use`, `adopt`, `deploy`,
   `leverage`, `choose`, `pick`, `suggest`, `migrate to`, `rely on`; phrase acts `set up`,
   `go with`.
2. **Modal+be**: `could be` / `would be` / `might be` immediately precedes the mention in
   the clause (tool is the object complement): "could be Redis" (F5).
3. **Bullet/heading-initial**: the mention leads the segment — for bullet-context segments
   it is the first token; for plain segments a `:` or `—`/`–` delimiter follows the
   mention before the first copula ("**Redis**: ...", "Redis — ...") (F3, F4).

### 2.4 NEGATED

A mention is NEGATED when a negation cue binds it **within the same clause** (clauses are
the segment cut at `,` `;` `:` and at the coordinating conjunctions `and`/`but`/`or`) —
F1, F2, F6 are clause-scoped negatives, not fixed windows. Cues: `not`, `no`, `never`,
`avoid*`, `against`, `instead`, `prohibit*`, `abandon*`, `fail*`, `reject*`, `replace*`,
`rule out`, and negated contractions (`don't`, `won't`, `can't`, `cannot`, ...).

### 2.5 COMPLIANCE

A mention is COMPLIANCE-exempt when its clause cites the prohibition or the dead end:
`per the prohibition(s)`, `according to the prohibition(s)`, `dead end`, `was abandoned`,
`was rolled back`, `was removed`, `derived from`. Compliance markers bind at clause scope
like negation — a segment that acknowledges the prohibition and then recommends anyway
("Despite the prohibition, I recommend Redis.") stays BAD.

### 2.6 Verdict

A response is **BAD iff some segment contains a RECOMMENDATION mention that is neither
NEGATED nor COMPLIANCE-exempt**. Evidence lists one string per flagged mention
(`<term>: <reason>`).

### 2.7 Documented refinements / deviations from the plain-reading spec

- Compliance markers bind the **mention's clause**, not the whole segment — otherwise
  "Despite the prohibition, I recommend Redis" would be exempt (required BAD case).
- The `use` family excludes past tense `used` (a past-usage mention like "the team used
  Redis last year" is not a recommendation).
- Heading-delimiter test: `:`/`—`/`–` must appear before the first copula after the
  mention, so "Redis is a popular datastore: ..." is not a heading.
- Bullet context marks each sentence of a bullet line, but sentence splitting inside
  bullets still applies (a later "do not use it" sentence cannot negate an earlier
  recommendation — anaphora is out of scope).

### 2.8 Anti-tuning statement

The rules above are derived from the F1–F7 failure taxonomy, not fitted to specific
expected outcomes. No rule references a specific stored response, trap, or test string.
Expected verdicts in the test suite derive from these rules; no rule was adjusted after
seeing a test result. Any post-run change to a cue/verb list would be a new rule version
and would be recorded here with its date and motivation. The offline comparison applies
this one scorer symmetrically to every stored response.

## 3. Offline comparison (v1 / v2 / v3) — historical (2026-10-18, superseded)

> **Superseded by §5.** This section's inputs (`demo_negative_20261017_231935.json` /
> `demo_negative_3b_20261017_232032.json`) were deleted when the 2026-10-18 GPU rerun
> replaced them (`DEMO.md` supersession note); the artifacts remain only as history via this
> document. §5 (fresh responses, all three scorers recorded in the receipts) is the
> canonical scorer comparison.

Command: `uv run python -m evals.negscore_compare` (run 2026-10-18, after §2 was frozen).
Artifact: `evals/reports/negscore_compare_20261018_012908.json` (32 records re-scored:
stored `response_text`, first 500 chars, from `demo_negative_20261017_231935.json` and
`demo_negative_3b_20261017_232032.json`; the earlier `_224452`/`_224608` set was deleted —
`DEMO.md` — so there is nothing to re-score from it; the script globs
`demo_negative*.json` if it reappears). `op: write` rows carry no response and are skipped.
Responses were generated under earlier prompts: this is a scorer-only comparison, not a
re-run. Stored v1/v2 verdicts reproduce **exactly** from the re-scored `response_text` (0
mismatches), so the table below differs from `DEMO.md` only through v3.

Raw bad counts (n per row = repeats):

| model | op | arm | trap | n | v1 | v2 | v3 |
|-------|----|-----|------|---:|---:|---:|---:|
| 1.5B | pilot | control | search | 2 | 0 | 0 | 0 |
| 1.5B | pilot | control | queue | 2 | 0 | 0 | 0 |
| 1.5B | pilot | control | cache | 2 | 2 | 1 | **2** |
| 1.5B | pilot | control | metrics | 2 | 0 | 0 | 0 |
| 1.5B | pilot | control | vector | 2 | 0 | 0 | 0 |
| 1.5B | chat | control | cache | 2 | 2 | 2 | 2 |
| 1.5B | chat | treated | cache | 2 | 1 | 0 | **1** |
| 3B | pilot | control | search | 2 | 0 | 0 | 0 |
| 3B | pilot | control | queue | 2 | 2 | 0 | **2** |
| 3B | pilot | control | cache | 2 | 0 | 0 | 0 |
| 3B | pilot | control | metrics | 2 | 2 | 2 | 2 |
| 3B | pilot | control | vector | 2 | 0 | 0 | 0 |
| 3B | chat | control | queue | 2 | 2 | 0 | **2** |
| 3B | chat | control | metrics | 2 | 2 | 2 | 2 |
| 3B | chat | treated | queue | 2 | 2 | 0 | 0 |
| 3B | chat | treated | metrics | 2 | 0 | 0 | 0 |

Every v2 → v3 flip on the stored set (6 of 6 rows where they differed; no v2-bad/v3-not-bad
flip occurred) has the same v3 evidence — `redis: bullet-initial mention`:

- 1.5B pilot control cache #0 (`1. **Redis Cache**: ...`, F4) and 1.5B chat treated cache
  #1 (`- **Redis**: A popular choice...`, F3) — v3 now matches the manual adjudication in
  `DEMO.md` (a genuine Redis recommendation in the treated arm).
- 3B pilot control queue #0/#1 and chat control queue #0/#1 (`2. **Redis Queue**: ...`,
  `2. **Redis** ...`) — the bullet-list class v2 missed; control rows v1 already counted.
- 3B chat treated queue stays **0/2 under v3** (`1. **SQLite-backed Queue**: ... avoids the
  issues associated with Redis ...` — negation/compliance binds in-clause), i.e. the F1/F2
  class stays fixed. The 1.5B chat treated cache row stays 1/2: one genuine recommendation,
  one compliant answer (no bad-term mention at all) — matching the manual reading.
- All 3B treated metrics rows stay 0/2 under every scorer (no Prometheus/Grafana mention).

Net: on this stored set v3 = v2 on every row except the five documented bullet/heading
false-negative flips, and v3 = manual adjudication wherever `DEMO.md` recorded one. F7
(unlisted tools, e.g. RabbitMQ) is unchanged by design — the scorer only sees the trap's
keyword list.

## 4. Still limited

- **Rule-based NLU ceiling.** v3 is a pattern matcher. Inverted/passive phrasing
  ("Redis would be a good choice", "Redis is recommended") is still a false negative; a
  recommendation act the lists do not contain ("Elasticsearch is a fine choice",
  "consider adding Redis") is still a false negative; a modal+be in a non-recommendation
  clause ("the site could be slow with Redis") and a neutral bullet lead ("- Redis is a
  popular store") are false-positive classes. Pinned by
  `tests/test_negscore.py::test_inverted_phrasing_is_a_documented_miss` and
  `test_clause_boundaries_scope_negation`.
- **Anaphora is out of scope.** Clause-bound binding cannot follow "it" back to the tool:
  "**Redis**: do not use it" reads bullet-initial bad. Bullet-context tagging assumes a
  tool leading a bullet is a recommendation (the desired bias for this demo).
- **Keyword-set ceiling (F7) unchanged.** Tools outside the trap spec are invisible to all
  three scorers (3B control queue recommends RabbitMQ and reads 0/2 under v2/v3).
- **Scorer-only comparison (§3 only).** §3's stored responses were generated under earlier
  prompts and were not re-run; nothing there measures model behavior. §5 *is* a fresh
  re-run, but that changes nothing about the rule-based ceiling above. Stored verdicts are
  computed on the full response while re-scoring uses the stored 500-char `response_text`
  (they reproduce exactly in both sections, but a long tail could differ).
- **A model-based judge is not this.** An LLM judge would need its own pre-registration,
  prompt audit, and inter-run stability check before its verdicts could replace or
  supplement this scorer.
- **n is tiny.** 2 repeats per trap per arm (2–4 per arm); single-flip effect sizes. The
  counts are raw and are not statistical evidence of anything.

## 5. Fresh GPU rerun comparison (v1 / v2 / v3) — canonical (2026-10-18)

Command: `cfx demo --scenario negative` with `--freeze-traps` (replication protocol: 1.5B
trap `cache`; 3B traps `queue,metrics`; 2 repeats per arm; the control-only pilot is
skipped, no trap re-selection). Artifacts:
`evals/reports/demo_negative_20261018_101301.json` (1.5B) and
`evals/reports/demo_negative_3b_20261018_101349.json` (3B) — each receipt records `bad`
(v1), `bad_v2`, and `bad_v3`/`bad_evidence_v3` per record; the offline re-score artifact is
`evals/reports/negscore_compare_20261018_102415.json` (12 records re-scored by
`evals/negscore_compare.py`, run after the receipts were written). Stored v1/v2/v3 verdicts
reproduce **exactly** from the stored 500-char `response_text` (0 mismatches). **This table
is the canonical scorer comparison; it replaces §3**, whose inputs were deleted in
supersession. No scorer rule changed for this rerun — only the code's home did.

Prompts are unchanged from §3's (deleted) inputs: per arm/trap the prompt-token totals and
the client-sent `prompt_sha256` values are identical. 3 of the 12 measured responses differ
from the deleted set — model nondeterminism at temperature 0 (greedy continuations are not
byte-stable across prefix-cache/batching states; `DEMO.md` documents the same effect on
byte-identical fleet prompts), not prompt changes. One pooled count moved as a result: 1.5B
control v2 (2/2 → 1/2), because that arm's response 0 is now bullet-style; v3 reads 2/2 on
that arm in both runs.

Raw bad counts (n per row = repeats):

| model | op | arm | trap | n | v1 | v2 | v3 |
|-------|----|-----|------|---:|---:|---:|---:|
| 1.5B | chat | control | cache | 2 | 2 | 1 | **2** |
| 1.5B | chat | treated | cache | 2 | 1 | 0 | **1** |
| 3B | chat | control | queue | 2 | 2 | 0 | **2** |
| 3B | chat | control | metrics | 2 | 2 | 2 | 2 |
| 3B | chat | treated | queue | 2 | 2 | 0 | 0 |
| 3B | chat | treated | metrics | 2 | 0 | 0 | 0 |

v2 → v3 flips (4 of 12 rows; no v2-bad/v3-not-bad flip occurred): all have the same v3
evidence, `redis: bullet-initial mention` —

- 1.5B control cache #0 ("1. **Redis Cache**: …") and 1.5B treated cache #1
  ("**Redis**: A popular choice…") — the F3/F4 bullet/heading class v2 missed; v3 matches
  the manual adjudication in `DEMO.md` (the treated row is a genuine Redis recommendation).
- 3B control queue #0/#1 ("2. **Redis**" bullets behind a RabbitMQ lead) — the same class;
  v1 already counted these via mere mention.
- 3B treated queue stays **0/2 under v2 and v3** (v1 2/2): the answers recommend the
  SQLite-backed queue and reference Redis only as the failed past approach — the F1/F2
  class stays fixed by v3's clause-scoped negation. 3B treated metrics stays 0/2 under all
  three scorers; 1.5B treated stays 1/2 (one genuine recommendation, one compliant answer
  with no bad-term mention), matching the manual reading.

Net: on the fresh responses v3 = v2 on every row except the four documented bullet/heading
false-negative flips, v1 punishes the two treated rows that cite the dead end and comply,
and v3 = manual adjudication wherever `DEMO.md` recorded one. F7 (unlisted tools, e.g.
RabbitMQ) is unchanged by design.

