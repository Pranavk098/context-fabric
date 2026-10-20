# B8 pilot — deviations from the pre-registration

Recorded as they are found, before the affected runs finish. Nothing here changes the sample,
arms, settings or criteria in `B8-PREREGISTRATION.md`.

## D1 — Parent hand-off is deterministic BM25 localization, not an LLM turn (found day 1, 2026-10-19)

- **What:** the harness's parent localization call posts to `{base_url}/v1/chat/completions`
  where `base_url` already ends in `/v1`, so the request goes to `/v1/v1/...` and fails. The
  harness then silently falls back to deterministic BM25 file ranking. Every cached parent
  hand-off in `evals/parent_handoff_raw/` has an empty `subagent_query` and `parent_notes`, and
  only `candidate_files` from the BM25 fallback.
- **When found:** after 2 of 30 task × arm runs (arm A on `django__django-11532` and
  `django__django-12262`). All 10 parent hand-offs had already been generated and cached.
- **Initial decision (day 1):** keep the cached hand-offs and do not fix the bug mid-run.
- **Revised decision (same day, by the user, before any A/B/C comparison was observed):** void the
  2 completed runs, fix the bug, and restart the pilot under `B8-PREREGISTRATION-v2.md` so every
  run of the result uses a real LLM parent. Void artifacts: `evals/reports/b8_void_20261019/`.
- **Superseded rationale for the initial decision:**
  All three arms of a task read the same cached hand-off, so the A/B/C comparison is unaffected.
  Fixing it now would give the remaining runs different hand-offs from the two completed ones.
- **Effect (void v1 runs only):** the "parent" was a deterministic code-search localizer with no
  model-written guidance. This does not apply to v2 runs.
- **Follow-up:** the URL join is fixed and the silent fallback removed before the first v2 run
  (parent failures are recorded as `parent_status: llm_failed` and the task is excluded).

## D2 — Parent call sent without an API key (found v2 day 1, 2026-10-20 UTC, before any arm ran)

- **What:** after the D1 fix, the parent localization call reached `cfx serve` but carried no
  `Authorization` header; the sidecar forwards client headers unchanged, so OpenAI returned 401.
  Because D1's fix removed the silent fallback, all 10 parents were correctly recorded as
  `parent_status: llm_failed` and **no arm ran**. `--grade` then crashed on the missing
  predictions file.
- **Decision (coordinator):** fix both before any v2 arm runs — the parent call sends the key from
  the environment (never logged), and `--grade` exits cleanly with nothing to grade; delete the 10
  failed parent caches and re-run the day's batch. No v2 result existed, so nothing is voided and
  the pre-registration is unchanged.

## D3 — Sub-agent never received the issue; parent saw an alphabetical file list (found by audit AU1, 2026-10-20 UTC, after 5 of 30 v2 runs)

- **What (AU1 F1, CRITICAL):** the sub-agent's task was the hand-off text alone
  (`agent.run(task=handoff_text)`). Arms A and C therefore received only code and never the
  problem statement; arm B saw it only through its packet's objective line; no arm received the
  parent's notes. That is a confound in favour of arm B, and arms A and C were not testing the
  pre-registered question.
- **What (AU1 F2, HIGH):** v2 §3 says the parent chooses from BM25-ranked candidate files; the
  code showed it an alphabetical file list truncated at 4,000 tokens, and the gold file was not in
  that view for 7 of 10 tasks.
- **Decision (coordinator):** void the 5 v2 runs (all arm A, 0 resolved; archived under
  `evals/reports/b8_void_20261020/`), fix the harness (F1–F15 plus code-audit H1/M1), have a
  separate agent re-audit it, and restart under `B8-PREREGISTRATION-v3.md`. No A/B/C comparison
  had been observed. The daily trigger now refuses to run until v3 and the fixes are committed.
- **Cost:** about 2.2M tokens of the free daily allowance (one day).
