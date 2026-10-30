# v4 pilot notes (run `v4-pilot-1`, 2026-10-30)

Unregistered pilot. Not evidence. 2 tasks (`v4-option-sections`, `v4-command-aliases`) x 5 arms x 1 rep,
Sonnet parent, Haiku children, per-run cap $1.00. Raw record: `evals/reports/v4_pilot/v4-pilot-1.json`.

## Gate checks

| Check | Result |
|---|---|
| Children spawn in every arm | Pass. 10 of 10 runs spawned exactly 1 child |
| `control` fails planted facts | Pass. 0 of 3 on both tasks |
| Zero `mcp_churn` | Pass. No infra failures |
| Brief v1 fires and fits its budget | Pass. Packet 2,372 bytes for a Haiku child (budget 6,000), 5 items, 1 excerpt, 3 constraints, no fallback |
| Children run on Haiku | Pass for `none`, `brief_v0`, `brief_v1`, `control` (`claude-haiku-5-5`) |
| `fork` child model | **Fail.** The fork child ran on Sonnet, the parent's model, and cost about 2x. The registered `brief_v1` vs `fork` cost comparison (0.52) is a model confound, not a context effect |
| Cost per run vs cap | Pass. Mean about $0.14 (fork $0.26). All 10 runs cost $1.39 in total, far under the $10 approval |

## What the pilot says about the benchmark

1. **The planted-fact test cannot separate the arms.** In all four arms with facts, planted-fact pass is identical
   (0.833). The parent states the facts in its own delegation prompt, so `none` already passes them. Brief v1 has
   nothing to add. As designed, the primary comparison would come out as a null for a reason unrelated to Brief.
2. **`resolved` is 0 of 10 everywhere**, so the pass-rate half of the non-inferiority test is degenerate. Two causes:
   - `section-helper` fails in every arm that has the fact, including `fork`. The fact or its test is probably wrong.
   - `v4-command-aliases` fails its hidden behaviour test in every arm, including `fork`. The test is probably
     too hard or wrong. Neither cause is confirmed. Run both hidden tests against `fixture_v4_reference` to check.
3. **The Agent call came back `async_launched`** for the brief_v1 child. That is the carry-back case.
   `carry_fired` was false in every run. Carry-back is still unverified.
4. Cost ratio `brief_v1` vs `none` is 0.997 (CI 0.988 to 1.007) on 2 tasks. That is noise, as expected.

## Before H3 (freeze) these need fixing

- Make the planted facts something the parent cannot simply copy forward. Options: state them to the parent in a
  way it is told not to repeat verbatim, or plant them in an earlier turn or file the child cannot see.
  Without this the benchmark has no power.
- Verify each hidden test against the reference solution; fix or drop the two failing checks.
- Force the `fork` child onto Haiku, or drop the model-matched claim and compare `fork` only on context size.
- Confirm what carry-back does with async-launched children.
