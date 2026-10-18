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
rendered).

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
  are the control-only trap pilot). `op: write` rows are the `POST /fabric/blocks` results.
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
26 blocks, broadcast blob 6687 o200k tokens (7449 Qwen tokens). Current artifacts:

- `evals/reports/demo_fleet_20261017_211536.json` (fleet; renderer already live in it)
- `evals/reports/demo_fleet_smoke_20261017_211752.json` (cold-cache reference)
- `evals/reports/demo_negative_20261017_213038.json` (1.5B, scorer v1+v2)
- `evals/reports/demo_negative_3b_20261017_213038.json` (3B probe, scorer v1+v2)

Supersession: the previous sets (`_204733` small fixture, `_210836`, `_210557`, and the
pre-v2 negative receipts `_211536` 1.5B / `_212021` 3B) are deleted. The fleet receipt was
not rerun for the scorer-v2 pass — the scorer only affects the negative scenario, and the
renderer fix was already live when it was captured. Earlier receipts also differ in one way
worth repeating: the renderer fix means the injected system message now ends with the
recency anchor, so compiled packets carry a few extra tokens per prohibition line (compile
fleet prompt +318 tokens over 6 requests) and the recency-anchored output contract shortens
compile completions (210 → 87 tokens) — older compile receipts are not token-comparable.

Fleet (6 objectives × 1 repeat per arm; broadcast ran first):

| arm | prompt tok | per req | completions | accuracy | wall | mean lat | cache hits (warm) |
|-----|-----------:|--------:|------------:|---------:|-----:|---------:|------------------:|
| broadcast | 44854 | 7476 | 188 | 6/6 | 2.82 s | 470 ms | 44816/44854 (99.9%) |
| compile | 13221 | 2204 | 87 | 6/6 | 1.43 s | 238 ms | 13152/13221 (99.5%) |

Compile sends **3.39× fewer prompt tokens at identical accuracy** (44854 → 13221, 31633
tokens saved across 6 requests) and finishes the arm faster (shorter prefills + shorter
answers). KV-cache peak sampling: 0.0605 broadcast / 0.0186 compile. The full-run cache
numbers are warm (the earlier smoke run sent byte-identical prompts); the cold-cache
reference is the smoke receipt: broadcast 37280/44854 (83.1% — first request cold, 5
identical repeats hit), compile 0/13221 (**0%** — every compiled packet is a distinct
prompt).

Negative, 1.5B (`_213038`; pilot control-only, 2 repeats per candidate; frozen trap
**cache** re-selected by the standing protocol — see caveat; treats the same prompt set as
`_211536`, so responses are byte-identical at temperature 0):

Pilot: `search 0/2; queue 0/2; cache 2/2; metrics 0/2; vector 0/2` → frozen trap **cache**
(1 trap × 2 repeats per arm; composition: control fabric = fixture only, treated = fixture
plus the cache dead end written via `POST /fabric/blocks`, seq 27; treated prompts end with
the `PROHIBITIONS` section, control prompts end with the fixture's unrelated prohibitions).

| arm | v1 bad | v2 bad | prompt tok | cache hits (warm) |
|-----|-------:|-------:|-----------:|------------------:|
| control (no dead-end block) | 2/2 | 2/2 | 4520 | 4512/4520 |
| treated (dead end written via POST) | 2/2 | 0/2 | 4658 | 2432/4658 (52.2%) |

The v2 flip is **not** compliance: inspecting the treated answers, the model writes
"consider adding a caching layer such as: 1. **Redis**..." — a genuine Redis recommendation
that v2 misses because `consider`/`adding` are not in the pre-registered verb list. 1.5B
shows **no genuine effect under either scorer** (v1 null; v2's treated zeroes are false
negatives).

Negative, 3B (`_213038`; `Qwen/Qwen2.5-3B-Instruct` at 0.85 utilization; pilot: `search 0/2;
queue 2/2; cache 2/2; metrics 2/2; vector 0/2` → frozen traps **queue, cache**, 2 × 2 per
arm; treated = fixture plus the two dead ends written via POST):

| arm | trap | v1 bad | v2 bad | prompt tok |
|-----|------|-------:|-------:|-----------:|
| control | queue | 2/2 | 2/2 | — |
| control | cache | 2/2 | 0/2 | — |
| treated | queue | 2/2 | 0/2 | — |
| treated | cache | 2/2 | 0/2 | — |
| control (pooled) | | 4/4 | 2/4 | 9004 |
| treated (pooled) | | 4/4 | 0/4 | 9532 |

Manual adjudication of the v2 rows (post-hoc reading, clearly not a scorer):

- **Treated queue 0/2 is genuine compliance**: "Given the prohibitions against Redis and
  Celery... it is recommended to continue using... **SQLite-backed queue**" — the model
  derived the correct resolution from the rendered prohibition, and v2 correctly does not
  count the negated mention.
- **Treated cache 0/2 is a v2 false negative**: "a suitable caching layer **could be** a
  distributed cache like Redis" — still recommending; `could`/`like` are not listed verbs.
- **Control cache 0/2 is a v2 false negative** too: the control answer recommends Redis in
  bullet lists without a listed verb before the mention.

So the honest bottom line: both scorers are mention-based instruments with opposite failure
modes — v1 counts negated mentions as bad, v2 misses recommendations phrased outside its
verb list ("consider adding ... such as", "could be ... like"). Under manual adjudication,
the **only genuine behavioral effect remains the 3B treated queue trap** (0/2 true
compliance vs control 2/2), consistent with the previous run's qualitative finding; nothing
else separates. Raw counts for both scorers are in the receipts; no scorer was changed after
these numbers were seen.

Honest caveats for any table this demo feeds:

- The fixture is **synthetic** (generated Project Atlas state); results say nothing about
  any real project.
- Broadcast is deliberately the strongest naive baseline (one identical blob ⇒ maximum
  prefix caching). The 3.39× input-token reduction is at this fixture size and model; it is
  not a universal constant.
- Accuracy checks are deterministic substring matches: they prove information parity, not
  answer quality. No LLM judge.
- Neither negative scorer is authoritative, and both are reported: v1 counts any mention of
  the bad keyword (including negations, punishing compliance); v2 — authored after observing
  v1 outputs, pre-registered before this rerun — misses recommendations phrased outside its
  fixed verb list ("consider adding ... such as", "could be ... like", bullet-list
  mentions), producing false negatives on both arms. The manual adjudication above is a
  post-hoc reading of the same responses, not a scorer, and should be treated as exploratory.
- The one genuine, repeatable effect is small and model-dependent: at 3B the treated queue
  trap complies (derives the SQLite-backed queue from the rendered prohibition) in both
  repeats; the 1.5B shows no genuine effect under either scorer. n = 2 repeats per trap per
  arm — a single-flip effect size.
- Trap selection is protocol-driven (control-only pilot), so the frozen set can change when
  the product changes (the renderer fix dropped `metrics` at 1.5B). No treated information
  ever enters selection.
- n is small (6 fleet requests per arm; 2–4 negative requests per arm); prefix-cache deltas
  are warm-cache and upstream-global; the pilot pre-warms the final control prompts.
- The 3B probe requires `--gpu-memory-utilization 0.85` (0.9 cannot boot while the Windows
  desktop holds ~1.1 GiB of VRAM); it is a separate artifact and labeled as a probe, not
  the headline number.

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
renderer fix: prohibitions render after every other section with the output contract last,
empty prohibitions skip the header, and `None` anchors still render.
