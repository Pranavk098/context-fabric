# Re-run Review — cold-boot fairness + KV gauge (2026-10-17)

Desk audit of the "honest re-run" commit-in-progress. **No GPU work was re-executed** and no
experiment code was changed: everything below is reconstructed offline from source, artifacts,
and pure-Python checks. Method: read `evals/audit_b6.py`, all seven artifacts, and every doc
that states the affected numbers; re-rendered both cold-boot prompts and re-scored the stored
answers with both the old and new scorer; recomputed every ratio from the JSONs.

---

## 1. Fairness verdict: the "identical information" claim is ACCURATE

Both arm prompts were reconstructed offline from construction lines verified verbatim in
`evals/audit_b6.py` (the script asserts each expression exists in the source before rendering).
The reconstruction reproduces the artifact token counts **exactly** (348 / 3,146), which is
independent evidence the rendered prompts are what actually ran.

**Information diff (symbol→module mapping extracted from each rendered prompt):**

```
holo pairs: 40   raw pairs: 40
mapping sets AND order identical (mapping diff: empty)
first pair holo: (0, 0)   raw: (0, 0)
last pair  holo: (39, 39) raw: (39, 39)
module tokens: holo 40  raw 40      symbol tokens: holo 40  raw 40
raw extra content: 200 "unrelated body content" lines, zero symbol/module references
token counts: holo=348  raw=3146  (artifact: 348 / 3146)  ratio=9.0402
```

- The raw arm carries the full `module_i -> symbol_i` mapping, one per file, in the same order
  and with the same identifiers as the hologram. Noise (5 helper lines × 40 files) contains no
  module/symbol references — verified programmatically.
- The scorer is a single `correct()` function applied to both arms (`evals/audit_b6.py:95-98,
  115-116`). Prompt construction differs only in the context block (`HOLOGRAM:` vs `FILES:`) and
  the documented body noise.
- Per-rep prompts get a `REP {rep}` tag applied identically to both arms; token counts in the
  artifact are for the untagged base prompts (tag adds ~4 tokens to both).

**Residual confounds (documented, not blockers):**
1. **Length/format is the treatment, not a confound:** raw is 9.0× longer and must be read as
   code listing rather than a mapping list. That is what the probe measures; it cannot attribute
   the raw arm's 25% to "retrieval failure" vs "long-context attention dilution".
2. **Scorer leading-context leniency** (see §2): `submodule3` would match `module3`; symmetric
   across arms, absent from all recorded answers, no material effect.
3. **Narrow probe:** n=4 questions × 3 reps, one 1.5B model, synthetic listing — stated in all
   three updated docs. No quality control beyond this probe was added.

## 2. Scorer verdict: old bug reproduces, fix works, one accepted leniency

Old scorer (`f"module{s}" in norm(answer)`) vs new (`re.search(rf"module{s}(?!\d)", norm)`):

| answer | symbol | old | new | note |
| :--- | ---: | :---: | :---: | :--- |
| `module_3` / `module_3.` / `module 3` / `The answer is module_3` | 3 | T | T | exact id, punctuation, space, sentence — all kept |
| **`module_39.py`** | 3 | **T** | **F** | the claimed bug, reproduced |
| `module_30` | 3 | T | F | trailing digit guarded |
| `module17` | 1 | T | F | same bug cross-symbol |
| `module_13` | 3 | F | F | no false positive either way |
| `submodule3` / `xmodule3` | 3 | T | T | leading-context leniency (accepted, see below) |
| `module_32` | 31 | F | F | artifact answer for symbol_31, correctly wrong |

**Provenance chain proves the 0.50→0.25 delta is scorer-only:** artifacts
`audit_b6_20261017_114951/115139` (repeats=1) and `115234` (repeats=3, the "cancelled attempt")
report raw 0.50 with the *2-3 calls scored by the old rule — their recorded model outputs
(`module_17.py`, `module_39.py`, `module_39.py`, `module_32`) are byte-identical to the
canonical `124755` run. Re-scoring those same answers offline: old rule 2/4 = **0.50**, new rule
1/4 = **0.25**. `124636`/`124755` (new rule) report 0.25. So the cancelled artifact is a scorer
regression witness, and the canonical artifact is the corrected score.

**Precision correction made:** the old B6.2 50% (artifact `173635`, mapping-free raw) was *not*
caused by the substring false positive — its two hits were `module_17` and `module_31.py`
(re-scored 0.50 under both rules). The ENGINEERING-LOG sentence claiming "half the old 50% was
the substring false positive" was inaccurate as history; it now attributes the 0.50→0.25 delta
to re-scoring the re-run answers, which is what the artifacts actually show.

**Accepted leniency (no code change):** the regex does not guard *leading* letters. A
`(?<![a-z0-9])` lookbehind cannot be added safely because the normalizer strips separators, so
legitimate sentences (`"The answer is module_19"` → `theanswerismodule19`) would stop matching.
No recorded answer contains a leading-letter variant; the leniency is symmetric. Documented here
and in `audit_b6.py`'s comment remains accurate ("exact identifier" modulo leading concatenation).

## 3. Numbers recomputed from artifacts (all match the docs)

| Number | Docs say | Recomputed | Artifact |
| :--- | :--- | :--- | :--- |
| Cold-boot tokens | 348 vs 3,146 → 9.0× | 3,146/348 = **9.0402×** | `audit_b6_20261017_124755.json` |
| Cold-boot accuracy | 100% vs 25%, 3 reps stable | 12/12 vs 3/12; per-rep [1.0/0.25]×3 | same |
| KV k=16 | 27.95% vs 1.38% = 20.2× | 0.2795341 / 0.0138231 = **20.2222×** | `stage_b5_20261017_124856.json` |
| KV "byte-identical ×3" | reproduces `153211` | KV **exactly equal** in `153211`/`115911`/`124856`; throughput varies (5.19/5.22/5.26; 44.76/42.07/43.25) | three stage_b5 reports |
| Throughput k=16 | 43.25 vs 5.26 = 8.2× | 43.2502/5.2575 = **8.2264×** | `124856` |
| F6 KV vs divergent | 13.48% vs 1.16% = 11.6× | 0.134775/0.011647 = **11.5714×** | `audit_fixes_20261017_115748.json` |
| F6 KV vs shared raw | 2.48% vs 1.16% = ~2.1× | 0.024830/0.011647 = **2.1319×** | same |
| Wrong-gauge story | misnamed gauge read **0** | `153023` reports `peak_gpu_cache_usage_perc: 0.0` on every case; 27.95% appears only under `kv_cache_usage_perc` from `153211` on | `stage_b5_20261016_153023.json` |

Every "20×" in the docs now carries its baseline: **vs unshared raw broadcast**, with the
~2× vs shared-raw qualifier attached or one sentence away (README table + caveat, CHANGELOG,
AUDIT, ENGINEERING-LOG, and the research docs).

## 4. Doc honesty verdict + corrections made

Verdict: **honest, with the corrections below.** The builder's core updates (README row +
caveats, log entry, N4/N6 rows, §C6 update, new §E) map to the right artifacts; old claims were
marked superseded rather than deleted; the wrong-gauge narrative is now factually correct (the
gauge error produced 0; the 20× comes from the `B0_divergent` baseline + 100 ms sampler).

Corrections made in this review (docs-only, no experiment code touched):

1. `README.md` — status rows: 2–9× row separated into "~2× vs best-case naive, ~9× vs realistic
   naive" (VERIFICATION-REPORT N1); throughput row gets the `B0_divergent` baseline; KV row
   gets "vs *unshared* raw broadcast (~2× vs a shared raw prefix)".
2. `CHANGELOG.md` — KV line gets the unshared-raw baseline and shared-raw qualifier; the
   "All 27 tracked gaps fixed or accepted; none rescoped" line corrected to the AUDIT's own
   tally (25 fixed / 1 accepted / GAP-08 + GAP-22 open) per N18.
3. `docs/engineering/VERIFICATION-REPORT.md` — N4 typo "Er" → "Err"; N1/N18 rows + §C5 marked
   with the 2026-10-17 corrections.
4. `docs/engineering/ENGINEERING-LOG.md` — J1 row gets the unshared-raw baseline; the
   "half the old 50%…" sentence replaced with the artifact-backed statement (see §2).
5. `docs/engineering/AUDIT.md` — 3,566/50% cold-boot line now carries the supersede pointer to
   N6; three "20×" instances get the unshared-raw baseline.
6. `docs/research/COMPETITIVE-LANDSCAPE.md`, `docs/research/PATH-VIABILITY.md`,
   `docs/research/SHARED-MEMORY-PRIOR-ART.md`, `docs/engineering/CHANGES-FROM-PROPOSAL.md` —
   stale/context-free "20×" and the obsolete "20× revised to ~9×" statement brought in line
   with N4/§E.

Stale-string greps: `3566` appears only in clearly-marked historical/superseded context;
`10× fewer tokens` / `2× accuracy` survive only inside the superseded N6 row and the B6.2
section with its inline supersede note; no `20×` remains without its baseline within the same
sentence/cell or the adjacent caveat.

## 5. Zombie files verdict

- The old headline cold-boot claim (348 vs 3,566 tok, 100% vs 50%) is from **`evals/audit_b6.py`**
  (pre-fix `section_coldboot`; artifact `audit_b6_20261016_173635.json`, whose raw prompt was 40
  file headers + 220 helper lines with **no symbol→module mapping**). It is *not* from
  `audit_fixes2.py`.
- `evals/audit_fixes2.py` runs a different, weaker probe (GAP-09 round 2: hologram 319/0.33 vs
  raw 2,039/1.00 in `audit_fixes2_20261016_161119.json`). No doc presents it as a valid current
  result: ENGINEERING-LOG round 2 labels it "OUTSTANDING (shortfall)" and rounds 3 + the honest
  re-run supersede the cold-boot line; AUDIT.md's final table cites the audit_b6 numbers (now
  marked superseded). The audit_fixes2 variant is historical only.
- The `audit_fixes2.py` `section_coldboot` remains dead code for any current claim; it is left
  in place as the historical round-2 artifact generator.

## 6. Checks run

- `uv run ruff check .` → **All checks passed**
- `uv run ruff format --check .` → **83 files already formatted**
- `uv run mypy protocol evals kernel sidecar` → **Success: no issues found in 38 source files**
- `uv run pytest -q` → **55 passed** (task brief said 52; the extra 3 come from new untracked
  test files in the working tree, unrelated to this re-run)

## 7. Remaining caveats — nothing provably broken, no GPU re-run needed

1. **Artifacts are not in version control.** `evals/reports/` is gitignored and *no* report is
   tracked (`git ls-files evals/reports` is empty). The four new JSONs referenced by the docs
   exist only locally; a fresh clone cannot verify any of these numbers. If the commit is meant
   to preserve the re-run evidence, force-add `audit_b6_20261017_124755.json`,
   `stage_b5_20261017_124856.json`, `audit_fixes_20261017_115748.json` (and optionally the
   cancelled `...115234.json` as the scorer-regression witness).
2. **Scorer leading-leniency** (`submodule3` matches) is documented, not fixed — any stricter
   regex needs tokenization, not a lookbehind, and no recorded answer is affected.
3. **`evals/audit_fixes.py` `range(0) → range(1)` fix is a genuine bug fix** (the old code
   would `KeyError` on `VALUES[0]`), endorsed; but the old F5/F6/F8 stored reports were produced
   by a code revision that does not match HEAD, so the untouched F5 "2.0×/9.0×" figures inherit
   that pre-existing provenance drift (flagged in VERIFICATION-REPORT already).
4. Untouched, separately-catalogued issues remain as recorded: N7 (quality proxy), N11–N14,
   N16/N17, N20's "1.4–2× throughput" phrasing, N21 arbitrage numbers, and the spec/code drift
   D1–D7. None are regressions of this re-run.
5. `CHANGELOG`/README still describe CoW forking as shipped narrative; VERIFICATION-REPORT §C7
   (CoW is prefix-cache sharing in the evals) remains the governing caveat.

**Blocking issues for the commit: none on correctness.** The only actionable item before
committing the evidence trail is (1) — decide whether the report JSONs should be force-added.
