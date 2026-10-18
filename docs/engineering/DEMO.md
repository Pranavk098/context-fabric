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
messages. A third mode, `--repo PATH`, replays the fleet story on a real local Python
repository with a fully auto-derived fixture (section below).

The second story — `--scenario negative` — exercises writable memory end to end. Candidate
traps (objectives whose tempting default answer is a specific bad action) are first piloted
on a control fabric *only*; candidates where the control never proposes the bad action are
discarded because they cannot discriminate. The frozen traps then get a known dead end
written through the *real* write path (`POST /fabric/blocks`, trusted) into the treated
fabric, and control vs treated are compared with the same three frozen scorers — v1/v2/v3,
all applied symmetrically to every response (`sidecar/negscore.py`; v3's pre-registered rules
in [NEGATIVE-SCORER.md](NEGATIVE-SCORER.md)). Raw counts are reported, never percentages
alone. If the pilot finds no discriminating candidate — or the treated arm shows no effect —
the receipt says so; a null result is a result. `--freeze-traps` instead pins the frozen
traps and skips the pilot, for replication runs that must stay comparable with the set they
replicate.

## Commands

```bash
# from the repo root (Windows: `uv run cfx demo ...` works too)
uv run python -m sidecar.demo --upstream http://127.0.0.1:8000 --model <name> --scenario fleet
uv run cfx demo --upstream http://127.0.0.1:8000 --model <name> --scenario negative \
  --out evals/reports/demo_negative.json

# replication run: pin a previous set's frozen traps, skip the control-only pilot
uv run cfx demo --upstream http://127.0.0.1:8000 --model <name> --scenario negative \
  --freeze-traps cache --out evals/reports/demo_negative.json

# no model server needed: in-process canned upstream, wiring/UX check only
uv run cfx demo --mock --price openai:gpt-4o-mini

# price a real run with a preset or custom rates (USD per 1M tokens)
uv run cfx demo --upstream http://127.0.0.1:8000 --model <name> --price anthropic:sonnet
uv run cfx demo --upstream http://127.0.0.1:8000 --model <name> --price "custom:2.5,10,1.25"
```

Flags (env fallbacks): `--upstream`/`CF_UPSTREAM` (default `http://127.0.0.1:8000` — a REAL
OpenAI-compatible endpoint), `--model`/`CF_MODEL` (default `default`), `--scenario
fleet|negative` (default `fleet`), `--out` (default `demo_receipt.json`), `--max-tokens`
(default 96), `--timeout` seconds (default 120), `--mock` (in-process fake upstream; no GPU,
network, or model server), `--price <preset|custom:IN,OUT,CACHED[,WRITE]>` (adds a `cost`
section; presets are listed in [COSTS.md](COSTS.md)), `--freeze-traps ID[,ID...]` (negative
scenario only: skip the control-only pilot and measure exactly these trap ids; rejected for
the fleet scenario and for unknown ids), `--repo PATH` (run the repo scenario on a local
Python repository instead of the synthetic fixture; rejected with `--scenario negative`).
Set `CF_PRICE_INPUT_PER_MTOK` to add
the legacy input-only `estimated_input_cost_usd` to the summaries (and to `/fabric/stats`);
without it the field is omitted. The legacy env var and `--price` are independent.

The demo builds a temp SQLite fabric with a deterministic **synthetic** project state
("Project Atlas", generated in `sidecar/demo.py` — no real data): 10 core index-card blocks
(3 decisions, 3 facts, 1 observation, 2 negative-knowledge entries, 1 quarantined draft)
plus 16 generated runbook/incident/interface/sprint-digest blocks, 26 blocks total. The
rendered broadcast blob is ~6.7k o200k tokens (7.4k under the Qwen tokenizer — comfortably
inside the 8192-token serving window); a regression test keeps it above 5000 tokens. The
demo starts two sidecars in-process on port 0 and talks to the upstream with `httpx`.

## Run it on your own repo

`--repo PATH` points the fleet story at any local Python repository: the same before/after
receipt on real code, with no hand-written objectives.

```bash
# wiring check, no GPU (canned upstream)
uv run cfx demo --repo . --mock

# real run, priced
uv run cfx demo --repo /path/to/repo --upstream http://127.0.0.1:8000 --model <name> \
  --price openai:gpt-4o-mini --out evals/reports/demo_repo.json
```

`sidecar/repofixture.py` builds the whole fixture automatically:

- Scan: `*.py` under `PATH`, sorted by relative path. Path parts in `.git`, `.venv`/`venv`/
  `site-packages`, `__pycache__`, `tests`, `build`/`dist`, tool caches, and `node_modules`
  are skipped. Files are taken until the running per-file o200k token sum reaches
  `raw_token_target` (default 6000; the crossing file is included), capped at `max_files`
  (default 40); a file over 20000 tokens is skipped as a vendored/generated outlier.
- Objectives: the real AST extractor's exported symbols drive six role questions of the
  form "In which file is `<symbol>` defined?" — distinct symbols are picked round-robin
  across the chosen files (public names preferred), so they spread across modules, and the
  answer key is the real relative file path.
- Fabric: one fact block per objective (module docstring first line + the symbol's real
  signature and path) plus the Tier-0 hologram, so compiled packets carry the real
  symbol→file map. The raw arm's system message is the actual file bytes with
  `===== FILE: <path> =====` headers.
- Determinism: same repo + same parameters → byte-identical fixture (no RNG, no wall
  clock; the `.cfx` cartridge id hashes the selected files' content).

Limits (v0): Python repositories only; path-key checks are case-insensitive substring
matches of the exact relative path (information presence, not semantic recall); the raw
arm size is approximate because whole files are added (the last file can overshoot the
target); `--repo` is fleet-style and rejected with `--scenario negative`. The receipt
carries `"fixture": {"kind": "repo", "path": ..., "files": N, "raw_tokens": X}`.

## Mock mode (`--mock`)

`--mock` starts a deterministic fake OpenAI-compatible endpoint in-process and points the
demo at it, so the full pipeline (fixture, both arms, receipt, artifact) runs with no GPU,
network, or model server. The first stdout line is

```
MOCK MODE — canned responses; accuracy is meaningless, this checks wiring/UX only
```

the JSON artifact carries `"mock": true`, and `notes[0]` says the same. Canned completions
are keyed by the objective's role (fleet) or trap id (negative); `prompt_tokens` is the real
`count_tokens` count of the messages the demo actually sent (including the server-rendered
packet in compile mode), while `completion_tokens` is a fixed 8. The fake endpoint exposes
no `/metrics`, so `cache.available` is `false` and the cost section bills all input at full
price. Mock accuracy is meaningless by construction: the canned fleet answers are generated
from the required substrings, and the negative arm is scripted to show the write path and
the control-vs-treated wiring. Never cite a mock receipt as a measurement.

## Costs (`--price`)

`--price` attaches a top-level `cost` object to the receipt and prints cost lines; without
it no cost section appears and behavior is unchanged. Accepted forms:

- a preset: `openai:gpt-4o`, `openai:gpt-4o-mini`, `anthropic:sonnet`, `anthropic:haiku`,
  `local:free` (preset table, sources, and checked date: [COSTS.md](COSTS.md));
- custom rates in USD per 1M tokens: `custom:IN,OUT,CACHED[,CACHE_WRITE]`, e.g.
  `custom:2.5,10,1.25`.

The model is token count × list price: uncached input at the input rate, cached input at the
cached-input rate, cache writes at the preset's write rate when it has one (otherwise the
input rate), completion tokens at the output rate. Cached tokens are estimated from the
vLLM prefix-cache counters as `round(prompt_tokens × hits/queries)` per arm; when the
upstream exposes no usable counters (non-vLLM, mock mode, zero queries) all input is billed
at full price. Cache writes are not measured and are 0 in receipts. `savings_pct` is
`(baseline total − compile total) / baseline total`, with the broadcast arm as the baseline
for fleet and the raw arm for repo; it appears only when both a baseline and a compile arm
exist (negative has neither). Costs are per arm, not per record. Example from a mock run
(`--mock --price openai:gpt-4o-mini`; values rounded here):

```json
"cost": {
  "preset": "openai:gpt-4o-mini",
  "per_arm": {
    "broadcast": {
      "requests": 6, "prompt_tokens": 40203, "completion_tokens": 48, "cached_tokens": 0,
      "total_usd": 0.006059, "per_request_usd": 0.001010
    },
    "compile": {
      "requests": 6, "prompt_tokens": 12832, "completion_tokens": 48, "cached_tokens": 0,
      "total_usd": 0.001954, "per_request_usd": 0.000326
    }
  },
  "savings_pct": 67.8,
  "assumptions": [
    "openai:gpt-4o-mini list prices checked 2026-10-18 (...); providers change prices — verify before relying on this.",
    "no usable cached-token counters; all input tokens billed at full price (cache writes not modeled).",
    "cost model is token-count × published list prices; provider billing quirks (Anthropic cache TTLs, minimums) are not modeled."
  ]
}
```

Each arm entry also carries the component dollars (`input_usd`, `cached_input_usd`,
`cache_write_usd`, `output_usd`). The printed table adds one `cost[arm]` line per arm, a
savings line when applicable, and the assumptions. The legacy `CF_PRICE_INPUT_PER_MTOK`
estimate (`estimated_input_cost_usd` in the summaries) is unchanged and does not include
cached or output pricing; prefer `--price` for total dollars.

## Reading the receipt

Top-level keys: `scenario`, `model`, `upstream`, `started_at`, `records`, `arms`, `cache`,
`notes`; `fixture` appears on repo runs, `cost` is present only with `--price`, and `mock`
only in mock mode (`true`). Everything printed as a table is also in the JSON.

- `records` — one dict per request: `op` (`chat`/`pilot`/`write`), `arm`, the objective,
  tokens from the upstream `usage`, latency, the response text, and the scoring fields:
  `passed`/`missing` for fleet; for negative, all three scorers —
  `bad`/`bad_keywords_hit` (v1), `bad_v2`/`bad_keywords_hit_v2`, and `bad_v3`/
  `bad_evidence_v3` (one `<term>: <reason>` string per flagged mention) — applied
  symmetrically to every response. `op: pilot` rows are the control-only trap pilot (absent
  in a `--freeze-traps` replication run). Chat/pilot rows also carry `response_text` (the
  first 500 chars of `response`) and `prompt_sha256`, the sha256 of the messages the client
  sent to the sidecar; in compile mode the system message is rendered server-side, so the
  injected packet is not part of that hash (it is deterministic and reconstructable from
  the fixture). `op: write` rows are the `POST /fabric/blocks` results.
- `fixture` — repo runs only: `kind` (`repo`), `path`, the number of files, and the exact
  raw-bundle token count. The objectives and their real file-path keys are in `records`
  (`required`).
- `arms` — per-arm totals: requests, prompt/completion tokens, wall and mean latency,
  `passed`/`total` (fleet), or `bad`/`bad_v2`/`bad_v3` and `total` plus `per_trap` counts
  (negative).
- `cost` — only with `--price`: preset label, per-arm dollars (total, per request, token
  counts, cached estimate, component dollars), `savings_pct` for fleet, and the
  assumptions list. See [COSTS.md](COSTS.md).
- `cache` — vLLM `/metrics` deltas per arm (`vllm:prefix_cache_hits_total` /
  `vllm:prefix_cache_queries_total`) plus a best-effort ~50 ms sampled peak of
  `vllm:kv_cache_usage_perc` (fraction of KV capacity). Non-vLLM upstreams get
  `"available": false` and are skipped, never faked. These are warm-cache numbers: the
  upstream cache persists across runs, so identical prompts from a smoke run show as hits.
- `notes` — the honesty rules baked in at generation time: synthetic fixture,
  strongest-broadcast baseline, substring scoring is not a quality claim, pilot protocol,
  cache caveats, small n. In mock mode the first note labels the canned responses.

## Scorer v2 (pre-registered 2026-10-17, before the rerun)

Honesty first: **v2 was authored after inspecting the v1 outputs** of the `_211536` / `_212021`
negative runs (which showed v1 scoring a substantive compliance — "Given the prohibitions
against Redis and Celery... SQLite-backed queue" — as a bad recommendation). It is therefore
an *exploratory instrument fix*, not a rule pre-registered before the first negative run,
and it is recorded here (with the exact mechanism below) **before** the rerun that applies
it. Both scorers run on every response and both counts are reported; v1 is untouched.
(Since 2026-10-18 a third scorer, v3, also runs on every response — see the consolidated
scorers section below.)

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

## Scorers v1/v2/v3 (consolidated 2026-10-18)

All scorer code now lives in `sidecar/negscore.py` (one module, three named functions:
`is_bad_answer`, `is_bad_answer_v2`, `score_v3` + the `score_v1`/`score_v2` wrappers);
`evals/negscore.py` is a re-export shim. Receipts record all three per record — `bad`/
`bad_keywords_hit` (v1), `bad_v2`/`bad_keywords_hit_v2`, `bad_v3`/`bad_evidence_v3` — and the
stdout table prints all three counts. v1/v2 rules are unchanged by the move; v3 is the
clause-scoped rule **pre-registered in [NEGATIVE-SCORER.md](NEGATIVE-SCORER.md)** (2026-10-18,
before its code existed; constants unchanged since), derived from the documented v1/v2
failure taxonomy rather than fitted to trap cases. `evals/negscore_compare.py` re-applies
all three scorers offline to the stored `response_text` and verifies the stored verdicts
reproduce (including v3).

## Results (2026-10-18, local RTX 5070 / vLLM)

Environment: WSL2, vLLM 0.31.0, `--enforce-eager --enable-prefix-caching --max-model-len
8192 --gpu-memory-utilization 0.85`, tmp=0.0, max_tokens=96, non-streaming;
`CF_PRICE_INPUT_PER_MTOK` unset (no cost fields). Main run `Qwen/Qwen2.5-1.5B-Instruct`;
3B probe `Qwen/Qwen2.5-3B-Instruct` at 0.85 utilization (see probe caveat). Fixture: 26
blocks, broadcast blob 6687 o200k tokens (7449 Qwen tokens). Current artifacts (one negative
set; the fleet receipts are unchanged from the 2026-10-17 session and kept as-is):

- `evals/reports/demo_fleet_smoke_20261017_231840.json` (fleet cold-cache reference; kept)
- `evals/reports/demo_fleet_20261017_231929.json` (fleet warm; kept)
- `evals/reports/demo_negative_20261018_101301.json` (1.5B; scorers v1+v2+v3 recorded)
- `evals/reports/demo_negative_3b_20261018_101349.json` (3B probe; scorers v1+v2+v3 recorded)
- `evals/reports/negscore_compare_20261018_102415.json` (offline v1/v2/v3 re-score of the fresh pair)

Supersession: the negative pair supersedes `demo_negative_20261017_231935.json` /
`demo_negative_3b_20261017_232032.json` (deleted; they recorded v1+v2 only); the earlier
`_224428` / `_224442` / `_224452` / `_224608` set had already been deleted before that.
Only the negative scenario was rerun, replicating the superseded set's frozen protocol
(same traps — `cache` at 1.5B; `queue`, `metrics` at 3B — 2 repeats per arm; `--freeze-traps`
skips the control-only pilot, so no trap re-selection happens). The sent prompts did not
change: per arm/trap the prompt-token totals are identical to the superseded set (1.5B
control/treated 4884/5022; 3B control/treated 9782/10294) and the client-sent
`prompt_sha256` values match it exactly. 3 of the 12 measured responses differ from the
superseded run's stored responses; the differences are model nondeterminism at tmp=0 (greedy
continuations are not byte-stable across prefix-cache/batching states — the fleet smoke note
below records the same effect on byte-identical prompts), not prompt or fixture changes. The
one pooled count that moved is 1.5B control v2 (2/2 → 1/2): control response 0 is now
bullet-style ("1. **Redis Cache**: …") instead of verb-style — the documented v2
bullet/heading false-negative class — while v3 reads 2/2 on that arm in both runs. Every
other arm/trap pooled count is unchanged. The kept 2026-10-17 fleet receipts' provenance
(post-audit-fix regeneration, recap-dedup, warm-cache smoke ordering) still applies to them.

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

Negative, 1.5B (`_101301`; traps frozen via `--freeze-traps cache` — no pilot this run;
1 trap × 2 repeats per arm; composition: control fabric = fixture only, treated = fixture
plus the cache dead end written via `POST /fabric/blocks`, seq 27; treated prompts end with
`PROHIBITIONS` + the contract + a `KEY CONSTRAINTS` recap that skips the dead end — its
12-word prefix is already rendered under `PROHIBITIONS` — and restates two fixture
decisions; control prompts end with the fixture's unrelated prohibitions and a recap of
fixture decisions):

| arm | trap | v1 bad | v2 bad | v3 bad | prompt tok | cache hits (cold-ish) |
|-----|------|-------:|-------:|-------:|-----------:|----------------------:|
| control (no dead-end block) | cache | 2/2 | 1/2 | 2/2 | 4884 | 2432/4884 (49.8%) |
| treated (dead end written via POST) | cache | 1/2 | 0/2 | 1/2 | 5022 | 2608/5022 (51.9%) |

Manual adjudication (post-hoc reading, not a scorer): control response 0 leads a numbered
bullet "1. **Redis Cache**: … Pros/Cons" and response 1 uses a verb ("Use an in-memory cache
like Redis or Memcached…") — both genuine Redis recommendations; v3 flags both
(`redis: bullet-initial mention`, `redis: recommendation verb 'use'`), v2 misses response 0
(F4 class) while v1 flags both by mere mention. Treated response 0 recommends "leveraging
its built-in row cache" — the alternative named by the rendered prohibition, with no
Redis/Memcached mention at all (all three scorers 0) — genuine compliance; response 1 leads
a bullet with "**Redis**: A popular choice for in-memory caching…" — a genuine Redis
recommendation that v1 (mention) and v3 (bullet-initial) flag and v2 misses (F3 class). So
under manual reading the 1.5B treated arm again produced one compliant answer and one
genuine Redis recommendation: **no clean behavioral effect at 1.5B** — the same conclusion
as the superseded set, with v3 now matching the manual reading on both flagged treated rows.

Negative, 3B (`_101349`; `Qwen/Qwen2.5-3B-Instruct` at 0.85 utilization; traps frozen via
`--freeze-traps queue,metrics` — no pilot; the same traps as the superseded set; 2 × 2 per
arm; treated = fixture plus the two dead ends written via POST, seq 27/28):

| arm | trap | v1 bad | v2 bad | v3 bad |
|-----|------|-------:|-------:|-------:|
| control | queue | 2/2 | 0/2 | 2/2 |
| control | metrics | 2/2 | 2/2 | 2/2 |
| treated | queue | 2/2 | 0/2 | 0/2 |
| treated | metrics | 0/2 | 0/2 | 0/2 |
| control (pooled) | | 4/4 | 2/4 | 4/4 | prompt tok 9782; cache 4880/9782 (49.9%) |
| treated (pooled) | | 2/4 | 0/4 | 0/4 | prompt tok 10294; cache 5392/10294 (52.4%) |

Manual adjudication (post-hoc reading, clearly not a scorer):

- **Treated queue 0/2 under v2/v3 is genuine compliance** (v1 2/2 counts mere mentions):
  both repeats recommend the SQLite-backed queue and describe Redis only as the failed past
  approach ("…avoids the issues associated with Redis, which had a significant data loss
  during a failover event") — derived from the rendered prohibition; v2 and v3 both ignore
  the negated mention, v1 does not (F2 class).
- **Control queue 0/2 under v2 is a scorer blind spot, not parity**: the control answers
  recommend RabbitMQ (a queue tool the pre-registered keyword lists do not contain) plus a
  "2. **Redis**" bullet; v2 misses both (F4 class + unlisted tool), v3 flags the Redis bullet
  (bullet-initial), v1 flags Redis by mere mention. Read side by side, treated complies
  (SQLite queue) while control proposes a third-party broker — an effect none of the keyword
  instruments can fully see.
- **Treated metrics 0/2 under all three scorers**: both repeats avoid Prometheus/Grafana
  entirely (no keyword mention) and answer with database-level observability
  (pg_stat_statements, pgBadger, pgAudit) — not the dead end's prescribed stats endpoint, but
  no deployment of the prohibited tools — while both control repeats still recommend
  Prometheus+Grafana under all three scorers. Unchanged vs the superseded set. History:
  the superseded set's treated metrics arm was itself a shift from the set before it (which
  had recommended the prohibited tools), and that shift was never cleanly attributable —
  the treated prompt was the only experimental change, but prefix-cache numerics can perturb
  greedy continuations at temp 0 (in the smoke run, prompts byte-identical to the fleet run
  produced different compile completions) — so the raw counts were reported without causal
  claims. This rerun replicates the compliant behavior; n = 2.

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
- None of the three negative scorers is authoritative; all are reported on every response
  (v1/v2/v3 recorded; v3's pre-registered rules are in
  [NEGATIVE-SCORER.md](NEGATIVE-SCORER.md)): v1 counts any mention of the bad keyword
  (including negations, punishing compliance); v2 — authored after observing v1 outputs on
  the earlier run, pre-registered before the 2026-10-17 rerun — has two demonstrated
  failure-mode classes; v3 fixes the bullet/heading class but remains a pattern matcher
  (documented limits in NEGATIVE-SCORER.md §4). Fixed 8-token negation window (v2 false
  *positive*): the earlier deleted `_224452` set's treated 1.5B response put `instead` just
  outside the window before `memcached` (recorded in `GAP-WAVE-MEASUREMENTS.md`; that artifact
  is deleted and was never committed); in this set no false positive occurred (treated 1.5B
  response 0 contains no bad-keyword mention at all, and response 1's v2 miss is the documented
  bullet class, not a window miss). Recommendations phrased outside v2's verb list (false
  *negative*): heading/bullet mentions — this set's 1.5B control response 0, 1.5B treated
  response 1, and both 3B control queue responses, all caught by v3's bullet-initial rule —
  and unlisted tools (control 3B queue: RabbitMQ → not detected by any scorer). The manual
  adjudication above is a post-hoc reading of the same responses, not a scorer, and should
  be treated as exploratory.
- The genuine behavioral signals in this set are small and model-dependent: at 3B the
  treated queue trap complies in both repeats (SQLite-backed queue derived from the
  rendered prohibition) while control recommends an unlisted broker; at 3B the treated
  metrics arm avoids the prohibited tools in both repeats (as in the superseded set; the
  attribution caveat on the earlier shift is recorded in the adjudication above); at 1.5B
  there is no clean effect under manual reading. n = 2 repeats per trap per arm —
  single-flip effect sizes.
- Trap selection is protocol-driven (control-only pilot), so the frozen set can change when
  the product changes (earlier renderer/recall revisions moved it). This rerun intentionally
  skipped the pilot (`--freeze-traps`) to keep the superseded set's traps, so no re-selection
  happened; no treated information has entered selection in any run.
- n is small (6 fleet requests per arm; 2–4 negative requests per arm); prefix-cache deltas
  are upstream-global. This negative run skipped the pilot, so control's cache deltas are
  cold-ish — ~50% (the first repeat of each prompt is cold, the second hits) — unlike the
  superseded set, where the pilot pre-warmed control to ~99.6%.
- The 3B probe requires `--gpu-memory-utilization 0.85` (0.9 cannot boot while the Windows
  desktop holds ~1.1 GiB of VRAM); it is a separate artifact and labeled as a probe, not
  the headline number.
- Chat records now carry `prompt_sha256` of the client-sent messages only: in compile mode
  the injected system packet is rendered server-side and is not covered by the hash (it is
  deterministic and reconstructable from the fixture).
- Provenance: the kept 2026-10-17 fleet artifacts were regenerated post-audit-fix on the
  frozen code — the renderer's recap-deduplication skip (GW-04) was the measurement-relevant
  change in that window, so treated prompts from before it are not token-comparable
  (5072 vs 5022 at 1.5B; 10454 vs 10294 at 3B). The 2026-10-18 negative rerun's per-arm
  prompt tokens and client `prompt_sha256` values are identical to the superseded pair's,
  so its prompts are byte-comparable with them; where this file cites superseded-set responses as
  history (the `_231935` / `_232032` text in the failure taxonomy of NEGATIVE-SCORER.md),
  those artifacts are deleted and the citations are historical records, not live data.
- Cost numbers are token-count × published list prices from the preset table (checked
  2026-10-18); provider billing quirks (Anthropic cache TTLs, minimums) are not modeled.
  Cached-token counts are estimated from request-level vLLM prefix-cache counters (not a
  token-level measurement), cache writes are not measured, and prices change — verify
  current prices before quoting a dollar figure. Mock-mode costs are doubly synthetic: the
  prompt token counts are real for the messages sent, but the completion token count is a
  fixed 8 and no cache counters exist.

## Tests

`tests/test_demo.py` runs the whole thing against a fake OpenAI-style upstream (no GPU, no
network): fixture token floor (>5000 o200k tokens) and compile parity (including the
rendered `PROHIBITIONS` section), receipt schema, both fleet arms and symmetric pass/fail
scoring, pilot selection plus control-vs-treated separation via the real
`POST /fabric/blocks` write, all three negative scorers recorded per record, the
`--freeze-traps` replication path (pilot skipped, named traps only), the null-result path,
and valid `--out` JSON. It also covers `--mock` end to end (banner, `"mock": true`, real
prompt-token counts, no upstream), `--price` on fleet and negative receipts, and rejection
of unknown presets and invalid `--freeze-traps` usage. `tests/test_negscore.py` unit-tests
scorer v3 (rule-boundary cases and the F1–F6 historical failures) and the v1/v2 wrappers
against the frozen rules in `sidecar/negscore.py`.
`tests/test_costs.py` unit-tests the cost model: exact hand-checkable arithmetic, cached
discounts, cache writes, preset and custom parsing, malformed specs, unknown presets, and
zero/None handling.
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
`tests/test_repofixture.py` builds tiny real repos on disk and verifies the repo fixture:
six objectives whose path keys are checked against the actual files with `ast`, byte
determinism across builds, the raw-token target and `max_files` cap, huge-file and ignored-
directory skipping, store round-trip, and the error paths (no Python files, non-directory,
no symbols). The demo tests add mock `--repo` end to end (artifact keys, both arms, real
paths in `required`), a priced repo receipt, and the `--repo` rejection cases.
