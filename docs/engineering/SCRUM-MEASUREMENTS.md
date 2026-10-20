# SCRUM-MEASUREMENTS — independent verification audit (2026-10-18 wave)

**Auditor role:** independent verification. **Method:** desk audit with offline recomputation of
every claim from (a) the stored receipts, (b) the still-present httpx clone at the preregistered
commit, (c) git-recovered superseded artifacts (`git show HEAD:<path>`), (d) the live Ollama
install on this machine, and (e) offline re-runs (mock demo, scorer re-scoring, test suite,
doc-number checker). **No GPU reruns were performed** and none were needed: every claim that
depends on model outputs was re-derived from the stored response bytes; nothing was found to be
provably broken in a way that would require re-running a model.

Repo state during the audit: the whole wave is uncommitted; `evals/reports/` is **gitignored**
(tracked artifacts were force-added historically), so the new receipts are on disk but not yet
in git — see "Handoff" at the bottom. The httpx pilot clone still exists at
`C:\Users\<user>\AppData\Local\Temp\opencode\httpx_pilot` with `HEAD ==
b5addb64f0161ff6bfe94c124ef76f6a1fba5254`, so the pilot fixture was re-audited against the
real bytes.

## Verdict summary

| # | claim | verdict |
| --: | --- | --- |
| 1 | Pilot (httpx raw vs compile, all numbers and the three criteria) | **recomputed / exact** — every number reproduces from the clone + artifacts; cartridge recompiled byte-identical; prereg ordering holds on filesystem evidence (limits stated) |
| 2 | Ollama portability (token identity, 3.17x, 6/6, cache degradation, 4k truncation, model digest) | **recomputed / exact** — prompt `sha256` values are identical to vLLM on 16/16 compared records; stale comparator pointer fixed |
| 3 | Fresh negative rerun + v3 (all per-model/arm/trap counts, prompt identity, verdict reproduction) | **recomputed / exact** — all counts reproduce, 12/12 prompts identical, 3/12 responses differ as claimed, 12/12 verdicts reproduce under re-scoring |
| 4 | Concurrency bench (`request_queue_size=256` before/after) | **recomputed / exact** — all 32 table cells match `_103444` (before) and `_103625` (after); interpretation wording verified (one minor N=1 nuance noted) |
| 5 | `cfx demo --repo . --mock` fixture | **reproduced offline / exact** — 5 files, 8,503 raw tokens, 6 objectives, cartridge `cfx_repo_f36b222b`; mock 0/6 labeled meaningless; the claimed numbers exist in the receipt, not yet in DEMO.md |
| 6 | Cross-doc pointer sweep + honesty checks | **verified with fixes** — 87 path citations scanned; 1 stale current pointer + 3 stale line pointers + 1 misattributed caveat found and fixed; SHADOW-CORTEX/ADAPTERS v0 limits verified honest |
| 7 | `scripts/check_docs_numbers.py` | **passes** — 56/56 checks, before and after the audit edits (does not flag the README 226 test count; that is display-only in NUMBERS.md — reported for the orchestrator) |

## 1. Pilot — raw httpx bundle vs compiled packet

Sources: `evals/reports/pilot_20261018_013151.json`, `pilot_smoke_20261018_013144.json`,
`pilot_cartridge_20261018_013151.json`, `evals/fixtures/pilot_fixture_20261018_013151.json`,
`evals/pilot.py`, `docs/engineering/PILOT-PREREGISTRATION.md`, live clone.

**Preregistration ordering (claim 1a).** `PILOT-PREREGISTRATION.md` has CreationTime ==
LastWriteTime = 2026-10-18 01:27:10 local (PDT, UTC−7) = **08:27:10 UTC**; `evals/pilot.py` was
created 01:28:30 / last written 01:29:10; the smoke receipt's `started_at` is
`2026-10-18T08:31:44Z` and the full run's is `2026-10-18T08:31:51Z`. So the on-disk ordering is
prereg → runner → smoke → full. CreationTime == LastWriteTime means no post-creation edit of
the prereg is visible on this filesystem. There is no other evidence of post-hoc edits: the
prereg's objectives/keys match `evals/pilot.py` and the receipts byte for byte, and the
artifact filenames' `0131xx` stamps are local-time `time.strftime` values, not UTC (the doc
header said "01:31 UTC"; fixed in PILOT.md).
*Limits (stated honestly):* the file is uncommitted, so mtimes are the only dating evidence; a
copied/rewritten file could reset them, and "frozen 08:26 UTC" (the file's own claim, 70 s
before its mtime 08:27:10) cannot be independently confirmed beyond that.

**Token accounting (claim 1b, recomputed from the artifact records).**

| quantity | recomputed | claim | notes |
| --- | ---: | ---: | --- |
| raw prompt tokens total | 193,366 = 32,227+32,228+32,226+32,229+32,229+32,227 | 193,366 | per-request range 32,226–32,229 (PILOT.md) verified |
| raw mean/req | 32,227.67 → 32,228 | 32,228 | |
| compile prompt tokens total | 5,391 = 897+900+895+901+901+897 | 5,391 | mean 898.5 → 899 as displayed |
| ratio | 193,366 / 5,391 = **35.8683** → 35.87 | 35.87 | criterion (a) `compile x 3 <= raw` → true |
| raw accuracy | 5/6 (ops failed: answered `httpx/cli/main.py`, missing `_main.py`) | 5/6 | re-scored all 12 responses with `sidecar.demo.score_answer` → identical verdicts |
| compile accuracy | 5/6 (tester failed: answered `20`, missing `100`) | 5/6 | criterion (b) `compile >= raw − 1` → true |
| errors | 0 error rows; sidecar raw {requests 6, compiled 0, errors 0}; compile {6, 6, 0, 5391}; `X-Fabric-Compiled` flags 6/6 | same | criterion (c) → true |
| cache deltas | raw hits 193,184 / queries 193,366 (99.9%); compile 896 / 5,391 (16.6%) | same | KV peaks 0.2588 / 0.00732 |
| wall / mean latency | raw 1,191.7 ms / 193.8 ms; compile 925.9 ms / 154.2 ms | same | warm-cache numbers, doc says so |

**Now stronger than claimed:** all 12 stored `prompt_sha256` values re-derive byte-identically
from the artifacts' own messages (raw = rendered bundle + objective; compile = objective only;
JCS canonical hash). This pins the stored prompts to the exact bytes.

**Fixture (claim 1c).** Re-reading the clone reproduced, file for file, every `sha256`, byte
size, and per-file tiktoken count in the manifest; sum of per-file tokens = 32,143 and the
rendered header-inclusive bundle = **32,259 o200k**, exactly the manifest value. The 116-token
difference is the 10 `===== FILE: ... =====` headers plus 9 blank-line separators. The frozen
"first 10 files" rule holds under the frozen rendering: the 9-file prefix renders to 28,594
tokens (< 30,000), the 10-file prefix to 32,259.
*Nuance found (not fixable — the prereg is frozen by its own anti-tuning rule):* the prereg
quotes "31,913 tiktoken o200k" for the prefix; that is the newline-normalized content count
(`read_text` semantics). The raw-bytes rule actually shipped counts 32,143 (content) / 32,259
(rendered). ≥30,000 holds either way, and the prereg explicitly pre-declared the manifest value
authoritative ("expect ≈32,0xx"), so this is a measurement-method footnote, not a criteria
problem. The claimed Qwen2.5-tokenizer counts (31,867 prereg; 7,449 demo blob) are **not
re-derivable offline** — `transformers` is not installed in this venv (residual gap).

**Cartridge (claim 1d).** Re-running the real compiler (`extract_tree` + PageRank +
`build_cartridge`, budget 600) on the clone reproduced **`cfx_httpx-pilot_b5addb64`,
`token_count` 542, `kv_prefix_hash sha256:e2e6c970…db09`**, and a hologram byte-identical to
`pilot_cartridge_20261018_013151.json`; the receipt's `cartridge` block equals the cartridge
artifact exactly. The smoke cartridge (`_013144`) is the same payload.

**Answer keys (claim 1e).** The receipts carry the full response texts. Every stored `passed` /
`missing` re-scores identically under the shared scorer against the prereg keys
(`_client.py`, `request`, `toomanyredirects`, `100`, `gzipdecoder`, `_main.py`), and each key's
source line cited in the prereg was verified in the clone (`_client.py:594` `class Client`,
`:1307` `class AsyncClient`; `_api.py:195`/`:304` `return request(`; `_exceptions.py:249`;
`_config.py:247` `DEFAULT_LIMITS(max_connections=100…)`; `_decoders.py:85` / `:383`;
`_main.py:313`).

**Smoke pass.** Raw 1/1 at 32,227 prompt tokens (the raw architect `prompt_sha256` is identical
to the full run's, so no prompt changed between smoke and full), compile 1/1 at 897, 0 errors,
1/1 compiled; the smoke raw cache was cold (hits 0, latency 5.13 s vs 0.19 s warm). The full
run began ~7 s after the smoke began, consistent with one sequential session.

## 2. Ollama — portability matrix

Sources: `ollama_fleet_20261018_100707.json`, `ollama_fleet_smoke_20261018_100551.json`,
`ollama_negative_20261018_100740.json`, `demo_fleet_20261017_231929.json`,
`demo_negative_20261018_101301.json`, live `ollama` CLI + model manifest.

**Fleet (recomputed from artifacts).**

| arm | prompt tok | completion | accuracy | wall | mean lat |
| --- | ---: | ---: | ---: | ---: | ---: |
| broadcast, vLLM | 44,854 | 188 | 6/6 | 2,797.7 ms | 465.2 ms |
| compile, vLLM | 14,170 | 139 | 6/6 | 2,075.3 ms | 345.8 ms |
| broadcast, Ollama | 44,854 | 186 | 6/6 | 10,915.7 ms | 1,818.2 ms |
| compile, Ollama | 14,170 | 103 | 6/6 | 2,113.6 ms | 352.2 ms |

Every cell above matches ENGINE-MATRIX's table. The claimed "byte-identical" prompts were
verified the strongest way available: the client-sent `prompt_sha256` values are **equal across
engines on all 12 fleet records and all 4 negative chat records** (16/16), not just the token
totals. Reduction 44,854/14,170 = 3.1654 → 3.17x, 30,684 tokens saved. `compiled` flags are
6 true / 6 false on both engines' fleet receipts. Q4_K_M vs fp16 explains the completion-token
differences (186/103 vs 188/139), as the doc says.

**Negative (Ollama).** control 4,884 prompt / 192 completion / v1 2/2 / v2 2/2 / v3 2/2;
treated 5,022 / 192 / v1 2/2 / v2 0/2 / v3 2/2 — matches the doc table, and matches the fresh
vLLM pair exactly on prompt tokens (and on the 4/4 `prompt_sha256`). The write record is
`POST → 201, blk_43ad7cf08bcc0814, seq 27`. The treated v3 "hits" are the documented class: one
response leads a bullet `- **Redis or Memcached**: These are not recommended…` → v3 evidence
`redis: bullet-initial mention`, both repeats. The Ollama run also executed the control-only
selection pilot (cache 2/2 bad → the same trap frozen).

**4k truncation (verified).** `ollama_fleet_smoke_20261018_100551.json`: every broadcast request
reports `prompt_tokens == 4,096` exactly (6 x 4,096 = 24,576 total) and broadcast accuracy is
0/6 (answers lost the fixture facts, e.g. port 8081 instead of 9091); compile requests were
unaffected (14,170 total, 6/6). This is exactly the hazard the doc tells.

**Graceful cache degradation (verified).** `cache.available == false`, all counters `null`,
caveat string present; `sidecar/demo.py:1232` prints
`cache: unavailable (upstream has no /metrics) - counters skipped` (reproduced live in the local
mock run). Nothing faked.

**Model metadata (verified live).** `ollama --version` → 0.19.0; `ollama list` →
`qwen2.5:1.5b  65ec06548149  986 MB`; `ollama show` → `quantization Q4_K_M`; the model-layer
blob is 986,048,512 bytes; the manifest config digest is
`sha256:377ac4d7aeefd5b870c9fccff9a6d4df36901d99fe3277c2f755bc401601ba1c` — all as
ENGINE-MATRIX states.

**Fix applied:** ENGINE-MATRIX cited the deleted `demo_negative_20261017_231935.json` as the
vLLM comparator; repointed to `demo_negative_20261018_101301.json`. Equivalence verified before
the edit: prompt totals 4,884/5,022 identical and all four per-record `prompt_sha256` values
identical between the old (git-recovered) and new artifacts. The following sentence's
"(2/2 → 0/2)" was also updated because the fresh vLLM control is now 1/2 (the documented bullet
class), while the Ollama run is 2/2 → 0/2 — same separation, stated for both.

## 3. Fresh negative rerun + scorer v3

Sources: `demo_negative_20261018_101301.json`, `demo_negative_3b_20261018_101349.json`,
`negscore_compare_20261018_102415.json`, `negscore_compare_20261018_012908.json`, superseded
pair recovered via `git show HEAD:evals/reports/demo_negative_20261017_231935.json` /
`…demo_negative_3b_20261017_232032.json`.

**Counts (recomputed; trap term lists from `sidecar.demo.negative_candidates`).**

| model | arm / trap | n | v1 | v2 | v3 |
| --- | --- | --: | --: | --: | --: |
| 1.5B | control / cache | 2 | 2 | 1 | 2 |
| 1.5B | treated / cache | 2 | 1 | 0 | 1 |
| 3B | control / queue | 2 | 2 | 0 | 2 |
| 3B | control / metrics | 2 | 2 | 2 | 2 |
| 3B | treated / queue | 2 | 2 | 0 | 0 |
| 3B | treated / metrics | 2 | 0 | 0 | 0 |
| 3B | control pooled | 4 | 4 | 2 | 4 |
| 3B | treated pooled | 4 | 2 | 0 | 0 |

All rows match the claim (1.5B control 2/1/2, treated 1/0/1; 3B control queue 2/0/2, metrics
2/2/2; treated queue 2/0/0, metrics 0/0/0; pooled control 4/2/4, treated 2/0/0) and the receipt
`arms`/`per_trap` blocks. Token totals: 1.5B 4,884/5,022; 3B 9,782/10,294.

**Prompt identity.** All 12 chat records' `prompt_sha256` equal the superseded pair's
(12/12), and per-record prompt tokens match. **Response divergence:** exactly 3 of 12 responses
differ — 1.5B control cache #0, 3B control queue #0, 3B control metrics #0 — all in control,
consistent with the documented temp-0 prefix-cache/batching nondeterminism.

**Verdict reproduction (claim: "every stored verdict reproduces under re-scoring").**
Re-scoring with `sidecar/negscore.score_v1/score_v2/score_v3` on the **full stored response**
and on the stored 500-char `response_text`: 12/12 fresh chat rows reproduce v1/v2/v3 *and* the
`bad_evidence_v3` lists exactly. The superseded pair reproduces v1/v2 exactly (0 mismatches on
both text lengths; v3 is absent there by design). `negscore_compare_20261018_102415.json` has an
empty `stored_verdict_mismatches` list and its summary equals an independent recomputation from
its own records; the earlier `…012908.json` likewise has zero v1/v2 mismatches and its 16-row
summary matches NEGATIVE-SCORER §3 exactly. v2→v3 flips on the fresh set: 4 records, all
`redis: bullet-initial mention`. (NEGATIVE-SCORER §5 calls this "4 of 12 rows"; the table has 6
pooled rows / 12 records — wording only, the counts are right.)

**Manual-adjudication snippets** quoted in DEMO.md were checked against the fresh response
texts: `1. **Redis Cache**:` (1.5B control #0), `Use an in-memory cache like Redis or
Memcached` (#1), `leveraging its built-in row cache` (treated #0), `- **Redis**: A popular
choice…` (treated #1) — all present.

**Fix applied:** DEMO.md's caveat attributed the v2 "`instead` just outside the window before
`memcached`" false positive to "the superseded set". The recovered `_231935`/`_232032` contain
no `instead` (and no treated `memcached`) at all; the quote is from the earlier `_224452` set
(deleted and never committed; recorded in `GAP-WAVE-MEASUREMENTS.md`). The caveat now names the
earlier set and its evidence source. NEGATIVE-SCORER §1's source line now marks the
`_231935`/`_232032` pair as deleted/git-recoverable (they were already marked in §3/§5).

## 4. Concurrency bench

Sources: `bench_sidecar_20261018_103444.json` (before), `bench_sidecar_20261018_103625.json`
(after), plus the uncited intermediate runs `…103154.json` / `…103602.json`,
`sidecar/server.py:537-559`, `evals/bench_sidecar.py`.

All 16 "before" cells in PERFORMANCE.md equal `_103444` (config `[1,4,16,64]`, M=20, 50 blocks,
Python 3.12.13, Windows-11-10.0.26300-SP0) and all 16 "after" cells equal `_103625`. The load-
bearing values:

| check | recomputed | claim |
| --- | --- | --- |
| before passthrough N=16 | 293.39 req/s, p99 533.84 ms | 293 req/s, p99 534 ms |
| before passthrough N=64 | 306.74 req/s, p99 2,057.14 ms, **23 ConnectError** | 307 req/s, 2,057 ms, 23 errors |
| before compile N=64 | 133.53 req/s, p99 2,076.86 ms, **31 ConnectError** | 31 errors |
| after passthrough N=16 / N=64 | 900.93 / 834.66 req/s, p99 40.70 / 166.38 ms, 0 errors | 901 / 835, 41 / 166 ms, 0 |
| after compile N=4/16/64 | 130.88 / 132.97 / 126.79 req/s | "~130 flat" (GIL) |
| N=1 compile minus passthrough p50 | after: 5.968 ms (before: 6.031) | "+6 ms" |

The fix is real and minimal: `_Server(ThreadingHTTPServer)` with `request_queue_size = 256`
(`sidecar/server.py:537-546`), used by `make_server` (`:559`); the doc's claim that
`daemon_threads`/`allow_reuse_address` needed no change is verifiable in the stdlib defaults it
cites. The variance statement in the doc ("N=1 moved between 424 and 992 req/s") matches the two
uncited runs exactly (`_103602` 423.93, `_103625` 991.96). Interpretation wording was checked
line by line: fake instantaneous upstream, sidecar-own cost only, Windows dev box, one
process/GIL, run variance, "bounds the sidecar's additive cost" — all present and accurate.
Minor nuance: "Compile stays ~130 req/s at every N" — the after-run N=1 cell is 109.6 req/s
(floored by the cold first cell); flatness holds from N=4. The table directly above shows the
exact numbers, so nothing is hidden; left as written. `tests/test_perf.py` re-ran clean at
5.09 / 13.73 / 62.55 ms (doc: 5.0 / 13.7–14.3 / 61 ms; fresh-session note 5.01 / 14.33 / 61.65).
`tests/test_bench_sidecar.py` (smoke, one N=4 cell) passes.

## 5. `cfx demo --repo . --mock` (offline reproduction)

Ran `uv run cfx demo --repo . --mock` on this working tree (no GPU, no network). Receipt stats:
`"fixture": {"kind": "repo", "path": ".", "files": 5, "raw_tokens": 8503}`; six objectives with
six symbols (`shared_prefix`, `flops_proxy`, `synth_repo`, `openai_system_message`,
`section_realrepo`, `obs_step`) and their real file-path keys; the note carries the Tier-0
cartridge `cfx_repo_f36b222b` (430-token hologram). Raw per-request prompts are 8,520–8,522
tokens = the 8,503-token bundle + the objective, i.e. the raw arm sends exactly the fixture
bundle; raw total 51,125, compile total 4,167, both 0/6 in mock mode — which is labeled
meaningless in three places (stdout banner, `notes[0]`, `"mock": true`). The numbers in the
scrum claim (5 files / 8,503 / 6 symbols / `cfx_repo_f36b222b`) are what the command actually
produces; note they are **not yet written into DEMO.md** — its `--repo` section documents the
parameters but no run numbers, so the doc neither contradicts nor confirms them until
finalization (nothing to fix, nothing overstated). A separate check reproduced DEMO.md's fleet
mock cost example exactly: broadcast 40,203 / compile 12,832 prompt tokens, $0.006059 /
$0.001954, savings 67.8%; the synthetic blob re-built offline is 6,687 o200k tokens with
26 blocks (test asserts >5000 only).

## 6. Cross-doc sweep for the new docs

Scanned every `evals/`, `docs/`, `tests/`, `sidecar/`, `kernel/`, `protocol/`, `adapters/`,
`scripts/` path citation in ADAPTERS.md, SHADOW-CORTEX.md, MMU-FOLD-FORK.md, COSTS.md,
NEGATIVE-SCORER.md, ENGINE-MATRIX.md, PERFORMANCE.md, PILOT.md, PILOT-PREREGISTRATION.md,
DEMO.md, NUMBERS.md: **88 unique citations** (after the pointer fixes below), all resolving on
disk, to a git-recoverable deleted file, or to example `--out` targets. Honesty checks passed:

- **SHADOW-CORTEX** "not wired / no auto-generation": no `sidecar/` module imports or calls the
  module (grep-verified); the compiler/packer/render routing it cites (`compiler.py:114,146,176`,
  `packer.py` ~180-187, `sidecar/render.py:79`) is real; its "12 tests" claim matches
  `tests/test_shadow_cortex.py` (12 test functions). ADR-0005 lines 25/34, Arch-Spec 21/55/74/85,
  Module-Implementation 33–43 all verified verbatim.
- **ADAPTERS** v0 limits: the four public builders exist with the documented shapes and the
  module imports no HTTP client; `tests/test_adapters.py` (11 tests) exists; `ADR-0004` and
  `docs/design/Framework-Adapter-Spec.md` exist.
- **PILOT** caveats (warm-cache, store-only compilation, declared cartridge limitation, n=6
  substring keys) all match the receipts and the prereg.
- **PERFORMANCE** interpretation wording and **ENGINE-MATRIX** "what transfers / what does not"
  table verified against artifacts.
- **NUMBERS.md** is consistent with the artifacts it cites: `check_docs_numbers.py` recomputes
  all strict entries → 56/56 OK.

Corrections were made in place only where a pointer/attribution was stale (list below). No doc
was found to claim more than its evidence. Prior-wave docs that cite artifacts from the
never-committed `_2244xx` set (`GAP-WAVE-MEASUREMENTS.md`, `REPO-CLEANUP.md`, `DOCS-AUDIT.md`,
`DEMO-REVIEW.md`) are historical records from the previous wave; their overlapping numbers
reproduce against the kept receipts, but the exact cited files are unrecoverable — flagged, not
edited.

## Corrections made by this audit

| file | change |
| --- | --- |
| `ENGINE-MATRIX.md` | vLLM negative comparator repointed `demo_negative_20261017_231935.json` → `demo_negative_20261018_101301.json` (old file superseded/deleted; equivalence verified) |
| `ENGINE-MATRIX.md` | negative-separation sentence updated: Ollama 2/2 → 0/2 vs fresh vLLM 1/2 → 0/2 (both under v2; one control completion is now the documented bullet style) |
| `PILOT.md` | header timestamp clarified: `20261018_0131xx` file stamps are local (UTC−7); receipts' `started_at` is 08:31 UTC |
| `PILOT.md` | artifact list de-ambiguated: full + smoke cartridge/fixture files listed explicitly |
| `DEMO.md` | v2 fixed-window false-positive caveat reattributed from "superseded set" to the earlier deleted `_224452` set (`GAP-WAVE-MEASUREMENTS.md` evidence); the superseded pair contains no `instead`/treated `memcached` |
| `NEGATIVE-SCORER.md` | §1 source line now marks `_231935`/`_232032` as deleted/git-recoverable |
| `SHADOW-CORTEX.md` | `render.py:79` → `sidecar/render.py:79` (no `kernel/router/render.py` exists) |
| `MMU-FOLD-FORK.md` | `Build-Plan-Validation-Gate.md:44` → `:51` (the EPOCH_FOLD quote's line) |
| `MMU-FOLD-FORK.md` | `Module-Implementation.md:84` → `:92` (the `ctx://obs/...` line) |

`README.md`, `CHANGELOG.md`, `NUMBERS.md`, `IMPLEMENTATION-STATUS.md` were not edited (the last
does not exist in this tree). Re-ran `check_docs_numbers.py` after all edits: 56/56 pass.

## Residual repro gaps (honest)

- **No GPU reruns** (by directive; nothing provably broken): all model-behavior claims are
  verified from stored bytes and scorer re-application, not re-sampled.
- **Qwen-tokenizer splits** (prereg 31,867; demo blob 7,449) are not re-derivable here —
  `transformers` is not installed. All o200k counts were recomputed; the Qwen-usage claims in
  the receipts (`usage.prompt_tokens`) match vLLM exactly, which is the load-bearing part.
- **Prereg dating** is filesystem-mtime evidence (CreationTime == LastWriteTime); uncommitted
  content cannot be cryptographically dated. Stated as a limit, not a discrepancy: all visible
  times order correctly (prereg 08:27:10 UTC < runner 08:28:30 < smoke 08:31:44 < full 08:31:51).
- **Artifact commits:** `evals/reports/` is gitignored; none of the new receipts are in git yet.
  Until they are force-added, the docs' cited sources of truth are on one machine only (the CI
  checker would also fail any NUMBERS.md row pointing at them once added — see handoff).
- **`GAP-WAVE-MEASUREMENTS.md`** cites `_2244xx` artifacts that were never committed and are
  unrecoverable; its numbers match the kept receipts where they overlap (verified for the fleet
  tokens/ratio/accuracy, smoke cache split, and the `_231935` counts).
- Uncited bench runs `…103154` (pre-fix harness with different error counts 11/40) and
  `…103602` (a patched run) sit in the reports dir; they are consistent with the doc's variance
  note but may confuse a future reader — no action taken.
- DEMO.md's manual adjudications are post-hoc by their own label; this audit verified the quoted
  text exists and matches the scorers, not the judgments themselves.

## Handoff — what the orchestrator must update at finalization

1. **Commit the new artifacts with force-add.** `git add -f` the new receipts
   (`pilot_*`, `pilot_cartridge_*`, `ollama_*`, `demo_negative_20261018_*`,
   `demo_negative_3b_20261018_*`, `negscore_compare_20261018_102415`,
   `bench_sidecar_20261018_103444`, `bench_sidecar_20261018_103625`) and keep the deletions of
   `demo_negative_20261017_231935.json` / `demo_negative_3b_20261017_232032.json` (docs mark
   them deleted; they stay recoverable from HEAD). `evals/fixtures/` is not ignored and will
   add normally. Decide whether `_103154`/`_103602`/the smoke receipts ship too.
2. **README test count:** the tree now collects **333 tests → 330 passed, 3 skipped** (2
   `fastembed` extra, 1 symlink-permission). README says 226 in three places (badge line 11,
   command comment line 93, tree line 126) and NUMBERS.md `test_count` is 226. Recount and
   update all together, per the registry's own rule.
3. **NUMBERS.md rows to add** (strict, with artifacts; all values re-derived above): pilot
   bundle 32,259 / raw 193,366 / compile 5,391 / 35.87x / accuracy 5/6 vs 5/6 / cartridge 542
   tokens `cfx_httpx-pilot_b5addb64`; Ollama equality 44,854 / 14,170 / 4,884 / 5,022 +
   "3.17x"; bench after passthrough 901 / 835 req/s, p99 41 / 166 ms, 0 errors; repo fixture
   5 files / 8,503 tokens / `cfx_repo_f36b222b`. Then re-run `scripts/check_docs_numbers.py`
   after the artifacts are committed (its artifact-existence checks read the working tree, so
   it passes now; CI needs them committed).
4. **README/CHANGELOG rows** (if the wave ships as headline content): pilot (5/6 vs 5/6,
   35.87x, three criteria PASS), Ollama portability row (3.17x reproduced, 16/16 prompt-hash
   identity, no `/metrics`), adapters (`adapters/raw_llm.py`, v0 OpenAI/Anthropic fragments),
   Shadow Cortex v0 (not wired — phrase it exactly so), MMU fold/fork (`cfx fold`/`cfx fork`),
   cost model (`--price` presets), `cfx demo --repo` (mock numbers only; never cite mock
   accuracy), concurrency hardening (`request_queue_size=256`).
5. **Do not re-open:** the prereg (frozen; the 31,913 vs 32,143/32,259 nuance is a method
   footnote), the superseded pair deletions (already announced in DEMO.md/NEGATIVE-SCORER.md),
   and the `CF_MODE`/backlog details in PERFORMANCE.md (verified accurate).
