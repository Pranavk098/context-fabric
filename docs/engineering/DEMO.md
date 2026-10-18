# `cfx demo` — productized evidence receipts

`cfx demo` is a single command that puts a real OpenAI-compatible model behind two product
stories and writes the results down. The first story — `--scenario fleet` — is the honest
A/B the project keeps asking for: six role agents (architect, implementer, reviewer, tester,
auditor, ops) each ask a question whose answer lives in exactly one fabric block. One arm
pastes the entire synthetic "Project Atlas" project state into every system message (the
"broadcast" baseline — the strongest naive thing an agent scaffold can do, because the
identical, stable-order blob maximizes the upstream's prefix cache). The other arm sends
only the objective and lets the sidecar compile a packet from the SQLite fabric. Both arms
hit the same model with the same sampling parameters and are scored by the same deterministic
substring check, so the receipt proves *information parity* under very different token
budgets, not answer "quality". Compiled packets now end with the recency anchor — a
`PROHIBITIONS` section distilled from negative-knowledge blocks, then the output contract
(`render_packet`; fixed 2026-10-17, previously the anchor was built by the packer but never
rendered) — and, since the same day's packing-quality pass, a final `KEY CONSTRAINTS`
recap of up to two of the most recent decision/negative-knowledge blocks (64-token
prefixes, skipping blocks already fully covered by the prohibitions; `CF_RECAP=0`
disables). Chat records also carry the raw `response_text` (first 500 chars of the
response; the full text stays in `response`) and a `prompt_sha256` of the client-sent
messages.

The second story — `--scenario negative` — exercises writable memory end to end. Candidate
traps (objectives whose tempting default answer is a specific bad action) are first piloted
on a control fabric *only*; candidates where the control never proposes the bad action are
discarded because they cannot discriminate. The frozen traps then get a known dead end
written through the *real* write path (`POST /fabric/blocks`, trusted) into the treated
fabric, and control vs treated are compared with the same keyword scorer. Raw counts are
reported, never percentages alone. If the pilot finds no discriminating candidate — or the
treated arm shows no effect — the receipt says so; a null result is a result.

## Commands

```bash
# from the repo root (Windows: `uv run cfx demo ...` works too)
uv run python -m sidecar.demo --upstream http://127.0.0.1:8000 --model <name> --scenario fleet
uv run cfx demo --upstream http://127.0.0.1:8000 --model <name> --scenario negative \
  --out evals/reports/demo_negative.json
```

Flags (env fallbacks): `--upstream`/`CF_UPSTREAM` (default `http://127.0.0.1:8000` — a REAL
OpenAI-compatible endpoint), `--model`/`CF_MODEL` (default `default`), `--scenario
fleet|negative` (default `fleet`), `--out` (default `demo_receipt.json`), `--max-tokens`
(default 96), `--timeout` seconds (default 120). Set `CF_PRICE_INPUT_PER_MTOK` to add
`estimated_input_cost_usd` to the summaries; without it the field is omitted.

The demo builds a temp SQLite fabric with a deterministic **synthetic** project state
("Project Atlas", generated in `sidecar/demo.py` — no real data): 10 core index-card blocks
(3 decisions, 3 facts, 1 observation, 2 negative-knowledge entries, 1 quarantined draft)
plus 16 generated runbook/incident/interface/sprint-digest blocks, 26 blocks total. The
rendered broadcast blob is ~6.7k o200k tokens (7.4k under the Qwen tokenizer — comfortably
inside the 8192-token serving window); a regression test keeps it above 5000 tokens. The
demo starts two sidecars in-process on port 0 and talks to the upstream with `httpx`.

## Reading the receipt

Top-level keys: `scenario`, `model`, `upstream`, `started_at`, `records`, `arms`, `cache`,
`notes`. Everything printed as a table is also in the JSON.

- `records` — one dict per request: `op` (`chat`/`pilot`/`write`), `arm`, the objective,
  tokens from the upstream `usage`, latency, the response text, and the scoring fields
  (`passed`/`missing` for fleet, `bad`/`bad_keywords_hit` for negative; `op: pilot` rows
  are the control-only trap pilot). Chat/pilot rows also carry `response_text` (the first
  500 chars of `response`) and `prompt_sha256`, the sha256 of the messages the client
  sent to the sidecar; in compile mode the system message is rendered server-side, so the
  injected packet is not part of that hash (it is deterministic and reconstructable from
  the fixture). `op: write` rows are the `POST /fabric/blocks` results.
- `arms` — per-arm totals: requests, prompt/completion tokens, wall and mean latency,
  `passed`/`total` (fleet), or `bad`/`total` plus `per_trap` counts (negative), and cost
  when priced.
- `cache` — vLLM `/metrics` deltas per arm (`vllm:prefix_cache_hits_total` /
  `vllm:prefix_cache_queries_total`) plus a best-effort ~50 ms sampled peak of
  `vllm:kv_cache_usage_perc` (fraction of KV capacity). Non-vLLM upstreams get
  `"available": false` and are skipped, never faked. These are warm-cache numbers: the
  upstream cache persists across runs, so identical prompts from a smoke run show as hits.
- `notes` — the honesty rules baked in at generation time: synthetic fixture,
  strongest-broadcast baseline, substring scoring is not a quality claim, pilot protocol,
  cache caveats, small n.

## Scorer v2 (pre-registered 2026-10-17, before the rerun)

Honesty first: **v2 was authored after inspecting the v1 outputs** of the `_211536` / `_212021`
negative runs (which showed v1 scoring a substantive compliance — "Given the prohibitions
against Redis and Celery... SQLite-backed queue" — as a bad recommendation). It is therefore
an *exploratory instrument fix*, not a rule pre-registered before the first negative run,
and it is recorded here (with the exact mechanism below) **before** the rerun that applies
it. Both scorers run on every response and both counts are reported; v1 is untouched.

Rule v2 (mechanical and symmetric; applies identically to every arm):

- Split the response into sentences (on `.` `!` `?` and newlines); tokenize each sentence
  into lowercased alphanumeric tokens (apostrophes kept, so `don't` is one token).
- A bad-tool mention (e.g. `redis`) is **BAD** when a recommendation/use verb appears in
  that sentence before the mention — `use`/`using`, `adopt`/`adopting`, `set up`,
  `deploy`/`deploying`, `leverage`/`leveraging`, `go with`, `recommend`/`recommending`,
  `rely`/`relying` — and **no negation marker** (`not`, `don't`, `avoid`, `against`,
  `abandoned`, `failed`, `prohibit`, `instead`) appears in the fixed 8-token window
  immediately before the mention in that sentence.
- Negated mentions ("do not use X", "prohibitions against X") and stated alternatives
  ("use SQLite instead of Redis" — `instead` sits in the window) are **NOT** bad.
  Acknowledging the prohibition and then still recommending the tool ("Despite the
  prohibition, I recommend Redis" — no marker adjacent to the mention; `prohibition` is
  not the marker token) **IS** bad.
- Known limitations, stated up front: passive constructions ("Redis is recommended") and
  recommendation verbs outside the listed set are not caught; the marker set is exact-token;
  the window is fixed at 8 tokens.

## Results (2026-10-17, local RTX 5070 / vLLM)

Environment: WSL2, vLLM 0.31.0, `Qwen/Qwen2.5-1.5B-Instruct`, `--enforce-eager
--enable-prefix-caching --max-model-len 8192 --gpu-memory-utilization 0.85`, tmp=0.0,
max_tokens=96, non-streaming; `CF_PRICE_INPUT_PER_MTOK` unset (no cost fields). Fixture:
26 blocks, broadcast blob 6687 o200k tokens (7449 Qwen tokens). Current artifacts (one
set; the smoke reference belongs to the same session):

- `evals/reports/demo_fleet_smoke_20261017_231840.json` (cold-cache reference; ran first)
- `evals/reports/demo_fleet_20261017_231929.json` (warm — warmth attributable to the smoke run)
- `evals/reports/demo_negative_20261017_231935.json` (1.5B, scorer v1+v2)
- `evals/reports/demo_negative_3b_20261017_232032.json` (3B probe, scorer v1+v2)

Supersession: this set supersedes the `_224428` / `_224442` / `_224452` / `_224608` set
(deleted) and was regenerated post-audit-fix on the frozen code. The only measurement-
relevant change in the window is the renderer's recap-deduplication skip (GW-04): the
written dead end no longer renders twice in treated prompts (previously `PROHIBITIONS` and
again the `KEY CONSTRAINTS` recap; now once, with the recap falling back to two fixture
decisions). Fleet prompt tokens are unchanged (44854/14170; no negative-knowledge block is
selected by the fleet objectives), and the smoke/fleet client messages are byte-identical
(equal `prompt_sha256` sets); treated negative prompt tokens moved 5072 → 5022 (1.5B, one
trap × 2 requests) and 10454 → 10294 (3B, two traps × 2 requests). Cross-set token
comparisons for treated prompts are therefore not meaningful; only within-set numbers are.
The run order was smoke first — so the fleet receipt's warm-cache counters have an exact,
kept provenance (the smoke receipt captured 7.5 s earlier).

Fleet (6 objectives × 1 repeat per arm; smoke first, then broadcast/compile):

| arm | prompt tok | per req | completions | accuracy | wall | mean lat | cache hits (warm) |
|-----|-----------:|--------:|------------:|---------:|-----:|---------:|------------------:|
| broadcast | 44854 | 7476 | 188 | 6/6 | 2.80 s | 465 ms | 44816/44854 (99.9%) |
| compile | 14170 | 2362 | 139 | 6/6 | 2.08 s | 346 ms | 14128/14170 (99.7%) |

Compile sends **3.17× fewer prompt tokens at identical accuracy** (44854 → 14170, 30684
tokens saved across 6 requests) and finishes the arm faster (shorter prefills). KV-cache
peak sampling: 0.0605 broadcast / 0.0201 compile. The full-run cache numbers are warm
(byte-identical prompts were sent by the smoke run 7.5 s earlier); the cold-cache
reference is the smoke receipt itself: broadcast 37280/44854 (83.1% — first request cold,
5 identical repeats hit), compile 0/14170 (**0%** — every compiled packet is a distinct
prompt).

Negative, 1.5B (`_231935`; pilot control-only, 2 repeats per candidate):

Pilot: `search 0/2; queue 0/2; cache 2/2; metrics 0/2; vector 0/2` → frozen trap
**cache** (1 trap × 2 repeats per arm; composition: control fabric = fixture only, treated
= fixture plus the cache dead end written via `POST /fabric/blocks`, seq 27; treated
prompts end with `PROHIBITIONS` + the contract + a `KEY CONSTRAINTS` recap that now skips
the dead end — its 12-word prefix is already rendered under `PROHIBITIONS` — and restates
two fixture decisions; control prompts end with the fixture's unrelated prohibitions and a
recap of fixture decisions).

| arm | v1 bad | v2 bad | prompt tok | cache hits (warm) |
|-----|-------:|-------:|-----------:|------------------:|
| control (no dead-end block) | 2/2 | 2/2 | 4884 | 4864/4884 (99.6%) |
| treated (dead end written via POST) | 1/2 | 0/2 | 5022 | 2608/5022 (51.9%) |

Control is genuinely bad in both repeats under both scorers ("Use an in-memory cache like
Redis or Memcached…"). Manual adjudication of the treated rows (post-hoc reading, not a
scorer): response 0 recommends "leveraging its built-in row cache" — the alternative named
by the rendered prohibition, with no Redis/Memcached mention at all (v1 0, v2 0), i.e.
genuine compliance; response 1 leads a bullet with "**Redis**: A popular choice for
in-memory caching…" — a genuine Redis recommendation that v2 misses (no listed
recommendation verb in that sentence; the documented heading-style false-negative class)
while v1 catches the mention. So under manual reading the 1.5B treated arm produced one
compliant answer and one genuine Redis recommendation: **no clean behavioral effect at
1.5B** — the same conclusion as the previous set, now without needing a scorer
false-positive adjudication to get there.

Negative, 3B (`_232032`; `Qwen/Qwen2.5-3B-Instruct` at 0.85 utilization; pilot: `search
0/2; queue 2/2; cache 0/2; metrics 2/2; vector 0/2` → frozen traps **queue, metrics**,
2 × 2 per arm; treated = fixture plus the two dead ends written via POST; the pilot froze
the same traps as the previous set):

| arm | trap | v1 bad | v2 bad | bad_keywords_hit (v1 / v2) |
|-----|------|-------:|-------:|----------------------------|
| control | queue | 2/2 | 0/2 | redis / — |
| control | metrics | 2/2 | 2/2 | prometheus+grafana / prometheus+grafana |
| treated | queue | 2/2 | 0/2 | redis / — |
| treated | metrics | 0/2 | 0/2 | — / — |
| control (pooled) | | 4/4 | 2/4 | prompt tok 9782; cache 9760/9782 (99.8%) |
| treated (pooled) | | 2/4 | 0/4 | prompt tok 10294; cache 5392/10294 (52.4%) |

Manual adjudication of the v2 rows (post-hoc reading, clearly not a scorer):

- **Treated queue 0/2 is genuine compliance** (unchanged from the previous set): the
  answers recommend the SQLite-backed queue and describe Redis only as the failed past
  approach ("…avoids the issues associated with Redis, which had a significant data loss
  during a failover event") — derived from the rendered prohibition; v2 correctly does not
  count the negated mention (v1 does, via mere mention).
- **Control queue 0/2 is a scorer blind spot, not parity**: the control answers recommend
  RabbitMQ (a queue tool the pre-registered keyword list does not contain); the only Redis
  hit is a list item, so v1 marks the row bad via mere mention and v2 correctly ignores it.
  Read side by side, treated complies (SQLite queue) while control proposes a third-party
  broker — an effect the keyword instruments cannot see.
- **Treated metrics moved vs the previous set**: both repeats now avoid Prometheus/Grafana
  entirely (no keyword mention, so v1 and v2 both read 0/2) and answer with database-level
  observability (pg_stat_statements, pgBadger, pgAudit) — not the dead end's prescribed
  stats endpoint, but no deployment of the prohibited tools — while both control repeats
  still recommend Prometheus+Grafana (v1 2/2, v2 2/2) where the previous set's treated
  arm did too. The treated metrics prompt is the only experimental change (shorter; the
  dead end renders once instead of twice) and at temp 0 the old prompt had produced the
  bad recommendation in both repeats — but prefix-cache numerics can perturb greedy
  continuations (in the smoke run, prompts byte-identical to the fleet run produce
  different compile completions), so the raw counts are reported without claiming the
  recap-skip caused the shift. n = 2.

Honest caveats for any table this demo feeds:

- The fixture is **synthetic** (generated Project Atlas state); results say nothing about
  any real project.
- Broadcast is deliberately the strongest naive baseline (one identical blob ⇒ maximum
  prefix caching). The 3.17× input-token reduction is at this fixture size and model; it
  is not a universal constant. The number moved (13221 → 14170) when the renderer and
  recall internals changed in the earlier revision window, while this regeneration left
  the fleet prompts byte-stable — compiled prompts are not stable across code revisions.
- Accuracy checks are deterministic substring matches: they prove information parity, not
  answer quality. No LLM judge.
- Neither negative scorer is authoritative, and both are reported: v1 counts any mention of
  the bad keyword (including negations, punishing compliance); v2 — authored after observing
  v1 outputs on the earlier run, pre-registered before the previous rerun — has two
  demonstrated failure-mode classes. Fixed 8-token negation window (false *positive*): the
  previous set's treated 1.5B response put `instead` just outside the window before
  `memcached`; in this set no false positive occurred (treated 1.5B response 0 contains no
  bad-keyword mention at all). Recommendations phrased outside its verb list (false
  *negative*): heading-style bullet mentions (this set's treated 1.5B response 1:
  "**Redis**:" heading) and unlisted tools (control 3B queue: RabbitMQ → not detected at
  all). The manual adjudication above is a post-hoc reading of the same responses, not a
  scorer, and should be treated as exploratory.
- The genuine behavioral signals in this set are small and model-dependent: at 3B the
  treated queue trap complies in both repeats (SQLite-backed queue derived from the
  rendered prohibition) while control recommends an unlisted broker; at 3B the treated
  metrics arm now avoids the prohibited tools in both repeats (changed vs the previous
  set, where it recommended them — see the adjudication above for the caveats on
  attributing that shift); at 1.5B there is no clean effect under manual reading. n = 2
  repeats per trap per arm — single-flip effect sizes.
- Trap selection is protocol-driven (control-only pilot), so the frozen set can change when
  the product changes (earlier renderer/recall revisions moved it); in this regeneration
  the pilot independently froze the same traps as the deleted set (only `cache` at 1.5B;
  `queue` + `metrics` at 3B). No treated information ever enters selection.
- n is small (6 fleet requests per arm; 2–4 negative requests per arm); prefix-cache deltas
  are warm-cache and upstream-global; the pilot pre-warms the final control prompts.
- The 3B probe requires `--gpu-memory-utilization 0.85` (0.9 cannot boot while the Windows
  desktop holds ~1.1 GiB of VRAM); it is a separate artifact and labeled as a probe, not
  the headline number.
- Chat records now carry `prompt_sha256` of the client-sent messages only: in compile mode
  the injected system packet is rendered server-side and is not covered by the hash (it is
  deterministic and reconstructable from the fixture).
- Provenance of this set: artifacts were regenerated post-audit-fix on the frozen code;
  treated prompts no longer duplicate long negative-knowledge recaps (the previous set
  rendered each written dead end twice — under `PROHIBITIONS` and again in the `KEY
  CONSTRAINTS` recap — so its treated prompt tokens are not comparable: 5072 vs 5022 at
  1.5B, 10454 vs 10294 at 3B).

## Tests

`tests/test_demo.py` runs the whole thing against a fake OpenAI-style upstream (no GPU, no
network): fixture token floor (>5000 o200k tokens) and compile parity (including the
rendered `PROHIBITIONS` section), receipt schema, both fleet arms and symmetric pass/fail
scoring, pilot selection plus control-vs-treated separation via the real
`POST /fabric/blocks` write, the null-result path, and valid `--out` JSON.
`tests/test_scorer.py` unit-tests the pre-registered v2 rule on crafted sentences:
affirmative recommendations (bad), "do not use X" and "prohibitions against X... use Y" and
"use Y instead of X" (not bad), "Despite the prohibition, I recommend X" (bad), neutral
contexts (not bad), and that v1 remains unchanged. `tests/test_render.py` covers the
renderer: prohibitions render after every other section with the output contract second-to-
last, the `KEY CONSTRAINTS` recap is last, capped at 2 blocks and 64 words, skips blocks
covered by prohibitions, honors `CF_RECAP=0`, and `None` anchors still render.
`tests/test_packer.py` covers λ-MMR: near-duplicate pairs lose to diversity under a tight
tier-1 budget, MMR-selected sets have lower average pairwise Jaccard than utility-greedy on
a crafted fixture, empty candidates pack cleanly, tier-1 output stays event-seq ordered, and
selection is deterministic. `tests/test_position_probe.py` pins the probe's rule placement
(head/middle/tail at equal token counts) and its scorer's pass/fail classes (no GPU).
