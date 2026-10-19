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
