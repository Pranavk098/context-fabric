# B8 Pre-Registration v3 — does a sub-agent on a small window do better with Context Fabric? (pilot, n = 10)

**Status: DRAFT — not frozen.** It is frozen (this line replaced by a UTC timestamp) only after
a separate agent re-audits the harness against it and a live smoke on a non-sample task passes.
This supersedes v2. v2 was voided after 5 of 30 runs (all arm A, 0 resolved) because audit AU1
found that the sub-agent never received the issue and that the parent saw an alphabetical file
list (deviation D3 in `B8-DEVIATIONS.md`). No A/B/C comparison had been observed. Void artifacts:
`evals/reports/b8_void_20261020/`. Sample, arms, settings and criteria are unchanged from v2;
§3 and §6 below describe the corrected harness.

## 1. Question (the original problem statement)

When a parent agent hands a task to a sub-agent whose context window is small, does Context Fabric
let the sub-agent resolve more real tasks, with fewer window overflows and fewer tokens, than a
naive hand-off?

This is a **pilot** (n = 10). It can show a large effect or a clear negative one. It cannot
establish a small effect, and no statistical-significance claim is made from it.

## 2. Tasks (fixed now)

- Dataset: `SWE-bench/SWE-bench_Verified` at pinned revision `78f471bf655a3137b2e8a75af1501690ec009ec3`
  (the dataset the grader uses); the runner asserts the 10 sampled ids match the expected list.
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
- Parent hand-off: one LLM localization turn per task (same model, `temperature=0`, through
  `cfx serve` with the same allow-list and daily ledger), shown BM25-ranked candidate files
  (ranked by the problem statement) within a 4,000-token prompt, returning candidate files, a
  `subagent_query` and `parent_notes`. Produced once, cached to disk with `parent_status` and
  provenance (prompt, token count, model, timestamp, harness commit); all arms of a task use the
  same cache. If the parent fails after one retry, the task's arms are not run and the task is
  reported as excluded; a cap hit during the parent phase retries the next day. Offline check
  before freeze: the gold file is in the parent's view for 6 of 10 tasks (reported, not tuned).
- Task header: every arm's sub-agent task starts with the **identical** header (problem
  statement + parent `subagent_query` + `parent_notes`). It is not counted in the hand-off slot;
  its token count is recorded per run.
- Arms:
  - **A, naive:** raw hand-off (parent's candidate files, truncated to the slot), no folding.
  - **B, Context Fabric packet:** the current compiled packet as the hand-off, no folding.
  - **C, Context Fabric hand-off + folding:** BM25 full-body chunks + shared repo map as the
    hand-off, plus observation folding (`--obs-fold`, last 3 tool outputs verbatim, k = 3).
- Patch: if the agent does not submit a patch, the working-tree `git diff` is taken, by the same
  rule for every exit status and every arm; how the patch was obtained is recorded.
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
  allowance). A task × arm starts only if at least 550,000 tokens remain today; otherwise the day
  ends cleanly. A run stopped by the cap anyway is `cap_stopped` and re-run **from scratch** on a
  later day; it is never resumed mid-run and is excluded from R, O and U.
- Usage is recorded in the ledger before each response is returned; streaming requests are
  refused while the ledger is on.
- A tooling failure (crash, grader error, transient network/API exception, infrastructure fault)
  on a task × arm is recorded as such, not as a finished run, and allows one re-run of that task ×
  arm, disclosed in the result. Nothing else is re-run.
- Every finished task × arm appends one row to a persistent receipt log; `evals/b8_aggregate.py`
  computes R, O and U per arm from that log and the official reports, excluding `cap_stopped`,
  `parent_failed` and tooling-failure rows.
- Arm C's fold store starts fresh for v3 and is checkpointed on shutdown; fold counts are recorded
  per run.
- Model outputs are never edited. Criteria, sample, arms and settings do not change after this
  freeze; a different configuration needs a new, separately dated pre-registration.

## 7. Known limits, stated up front

- n = 10 with one model. Every arm may resolve few tasks.
- Arms run arm-major per invocation (all remaining A, then B, then C), so an arm's runs may span
  different days; upstream model behavior is assumed stable over the run window.
- The smoke task showed arm C exhausting the step limit without overflowing; that is a known risk
  for C, not something this design corrects.
- The parent's BM25-ranked view contains the gold file for only 6 of 10 tasks (offline check before freeze);
  for the other 4 the sub-agent must find it by exploration. This applies equally to all arms.
