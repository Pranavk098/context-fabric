# Tester-build report (2026-10-31 → 2026-11-01)

Overseer-authored. Branch `s13-integration`, built on `main` at the end of Sprint 12. Nothing is pushed
and no tag exists. Evidence below means a commit plus tests, not a CI run on GitHub (CI has not run
on any of this yet).

## Muse stop-ship checklist

| ID | Status | Evidence |
|---|---|---|
| M-0 Sprint 12 on main, diet delta | Done locally; **not pushed** | R-5 diet delta (priced at cache-read rates, labelled an estimate) and Sprint 12 merged; full suite green |
| M-1 `carry_v2` flag + golden | Done | flag default off; golden test: off reproduces v1 byte for byte |
| M-2 defaults test + doc fix | Done | `tests/test_defaults.py` pins every default, including `carry_v2`, `brief_excerpts`, `coach_control` |
| M-3 numeric `--share` | Done | `tests/test_share_numeric.py` (canary strings); digests only behind `--share-digests` |
| M-4 excerpt provenance + opt-in | Done | `tests/test_brief_provenance.py`: read ranges only, nothing outside the git work tree or from secret-looking names, default off. **Gap found and fixed at overseer review:** the worker added the flag but the daemon never passed it, so excerpts were still on whenever `brief_v1` was on. `brief.py` now passes `brief_excerpts`, `render` defaults to off, and a hook-level test proves it |
| M-5 SECURITY.md | Done (overseer) | state dir "private, not content-free", numeric `--share`, Brief excerpt surface, carry v2 4 KB, ACL as a stated assumption |
| M-6 README | Done (overseer) | v2 benchmark voided (linked), v4 unregistered, retired features marked, Gate 0 findings stated without new digits |
| M-7 retired means retired | Done | fold/bound/checkpoint print a retired notice, gone from help/tour/guide; daemon stops writing `obs/` and clears old blobs; ADR-0013 |
| M-8 golden transcripts + replay | Done in code; **workflow never run on GitHub** | `tests/golden/`, `tests/test_golden_accounting.py`, `.github/workflows/replay-check.yml` |
| M-9 feedback form | Done (overseer) | `.github/ISSUE_TEMPLATE/tester-feedback.yml`: consent, friction log, Likert per feature, leak checkbox; test added |
| M-10 one-pagers | Done (Gemini draft, overseer checked every command against `--help`) | `docs/testers/` (checkup, diet, fidelity, brief-opt-in, index). `cfxd fidelity --share` does not exist and the guides say so |
| M-11 tagged build | **Not done** | `install-smoke.yml` is written and its structure is tested, but it has never run. It needs a pushed tag |
| M-12 triage owner | **Founder** | needs a name with stop-ship authority |

## Offline experiments against the frozen Gate 0 bars

Full table in `gate-0-results-2026-10-31.md`. Summary: **E-1, E-2, E-3, E-4 failed their bars; E-5 and E-6 passed;
E-7 failed the strict bar** (fold blobs), which M-7 fixes. Net effect: Brief stays an opt-in experiment with no tester
claim, carry v2 stays off, churn never leads, and the tester build leads with diet and measurement.
F-1b reconciled the two tools that measure re-discovery share: `handoff-stats` and the E-1 denominator tool now both
give the same cost share over the same 54 sessions.

## Tester features

| ID | Status |
|---|---|
| F-1 `cfxd handoff-stats --share` | Done; matches E-1 and the E-3 sweep on the maintainer's logs |
| F-2 `cfxd checkup` | Done; order diet, fidelity, sub-agents; no churn section; ends "safe to paste" |
| F-3 `brief --share` numeric | Done under M-3 |
| F-4 coach random-half control | Done, behind `coach_control` (default off), counts-only receipts |
| F-5 install smoke | Written, **not run** |

## Tester commands run for real

On the maintainer's machine, from the integration branch: `cfxd checkup` (7d, 30d, all), `cfxd handoff-stats --share`,
the six E-scripts. Not run: `diet --apply`, the Brief opt-in session, install on a clean machine, anything on macOS
or Linux. "Each tester command at the candidate tag" is therefore **not met**.

## Blocked, and what only the founder can do

1. **Push `main`/`s13-integration`** (outward-facing; I am waiting for your go). Run `gh auth switch --user Pranavk098` first.
2. **Create the tag** and watch install-smoke on three OS; fix whatever it finds. Mac is still unverified.
3. **Name the triage owner** (M-12).
4. **Decide the headline.** The evidence supports "see where your tokens go, and trim your setup", not "we save you
   tokens". The first tester round should be measurement-only plus the opt-in Brief session.
5. The Sonnet direction check (~$5) and any paid benchmark remain unapproved, and the evidence now argues against a
   paid Brief run.
