# B8 Pre-Registration — does a sub-agent on a small window do better with Context Fabric? (pilot, n = 10)

**Status: PRE-REGISTERED. Frozen 2026-10-19 (UTC), before any of the sampled tasks below was run.**
The only live runs before this freeze are the harness smoke on `astropy__astropy-7606`
(`evals/reports/b8_smoke_report.md`), which debugged the harness and is not part of this result.
If the seeded draw below contains that instance, it is run fresh like every other task; its smoke
outcome is not reused.

## 1. Question (the original problem statement)

When a parent agent hands a task to a sub-agent whose context window is small, does Context Fabric
let the sub-agent resolve more real tasks, with fewer window overflows and fewer tokens, than a
naive hand-off?

This is a **pilot** (n = 10). It can show a large effect or a clear negative one. It cannot
establish a small effect, and no statistical-significance claim is made from it.

## 2. Tasks (fixed now)

- Dataset: `SWE-bench/SWE-bench_Verified` (the dataset the grader uses).
- Sample: `evals.b8_live.sample_instances(seed=20261019, n=10)`, a deterministic repo-stratified
  draw (Hamilton allocation proportional to repo size). The instance ids are written to the first
  receipt and must not change between days.

## 3. Setup (fixed now)

- Sub-agent: `mini-swe-agent` DefaultAgent, model `gpt-4.1-mini`, `temperature=0`,
  `step_limit=35`, `max_tokens=1024`, 10,000-character per-command output cap. Every request goes
  through `cfx serve`.
- Window: hard cap of **24,576 input tokens** per request (`--window-cap`); exceeding it ends the
  run as an overflow (`ContextWindowExceededError`).
- Hand-off slot: 8,192 tokens (`--handoff-budget`).
- Parent hand-off: produced once per task by the harness and cached to disk; all three arms of a
  task receive the same cached parent output.
- Arms:
  - **A, naive:** raw hand-off (parent's candidate files, truncated to the slot), no folding.
  - **B, Context Fabric packet:** the current compiled packet as the hand-off, no folding.
  - **C, Context Fabric hand-off + folding:** BM25 full-body chunks + shared repo map as the
    hand-off, plus observation folding (`--obs-fold`, last 3 tool outputs verbatim, k = 3).
- Grading: official `swebench.harness.run_evaluation` in Docker; `resolved` comes from its
  `report.json`. An empty patch counts as unresolved.

## 4. Measures

Per task × arm: resolved (0/1), overflow (0/1), steps, billed / cached / uncached tokens, exit
status. Per arm: total resolved, total overflows, total billed and uncached tokens, uncached tokens
per resolved task.

## 5. Criteria (fixed now; applied to B vs A and to C vs A separately)

Let R, O and U be an arm's resolved count, overflow count and total uncached tokens.

- **STRONG SIGNAL:** R(X) ≥ R(A) + 3.
- **SIGNAL:** R(X) ≥ R(A) **and** O(X) ≤ ⌈O(A) / 2⌉ **and** U(X) ≤ 0.75 · U(A).
- **NEGATIVE:** R(X) < R(A) − 1.
- **INCONCLUSIVE:** anything else.

Also reported, not as a criterion: an exact McNemar test on paired resolved outcomes for each
comparison, and B vs C.

## 6. Execution and errors

- A daily token ledger caps spend at 2,200,000 tokens per UTC day (inside the free 2.5M mini-model
  allowance). A run stopped by the cap is recorded as `cap_stopped` and re-run **from scratch** on
  the next day with `--resume`; it is not counted as a failure and is never resumed mid-run.
- A tooling failure (crash, grader error, infrastructure fault) on a task × arm allows one re-run
  of that task × arm, disclosed in the result. Nothing else is re-run.
- Model outputs are never edited. Criteria, sample, arms and settings do not change after this
  freeze; a different configuration needs a new, separately dated pre-registration.

## 7. Known limits, stated up front

- n = 10 with one model. Every arm may resolve few tasks.
- Arms run arm-major per invocation (all remaining A, then B, then C), so an arm's runs may span
  different days; upstream model behavior is assumed stable over the run window.
- The smoke task showed arm C exhausting the step limit without overflowing; that is a known risk
  for C, not something this design corrects.
