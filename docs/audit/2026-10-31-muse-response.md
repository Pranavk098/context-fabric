# Muse audit response: decisions taken (2026-10-31)

Muse audited `main` at `d330a5f`. Its full reply is kept by the founder. This file
records what we accepted and where each item went in
`docs/plans/2026-10-31-delivery-status.md` (Parts 2–3).

| Muse point | Decision | Where |
|---|---|---|
| Sprint 12 numbers unverifiable (branch unpushed) | accept: merge to `main` first | M-0 |
| ADR-0012 not on main | accept: lands with M-0 | M-0 |
| README benchmark line stale | accept | M-6 |
| Default contradiction (config docstring vs packet) | **clarify**: `brief` (v0 pointers) is on and `brief_v1` is off, so both statements were partly right; fix the docstring and assert all defaults in one test | M-2 |
| Risk 1: wrong denominator | accept; no product claim from Gate 1 until E-1 | E-1 |
| Risk 2: n=1, tuned on reported logs | accept: freeze criteria; tester logs are the held-out set | Gate 0, F-1 |
| Risk 3: excerpt injection | accept: provenance test, skip outside git, per-repo opt-in | M-4, M-5 |
| Risk 4: tester trust | accept | M-1, M-3, M-5, M-6 |
| Risk 5: cheap builders on measurement code | accept: golden transcripts, replay CI, overseer authorship | M-8, authorship rule |
| `--share` digests unsafe for public posts | accept: numeric by default | M-3 |
| Encryption at rest | not needed if E-7 confirms no stored text | E-7 |
| Lead with diet; fidelity second; churn after E-2 | accept | F-2 |
| Brief off by default with a structured opt-in task | accept | M-2, M-10 |
| Coach: precision-first plus a random-half control | accept | F-4 |
| PR curve instead of single-point precision | accept | E-3 |
| Carry per-byte bars; ask why selection didn't improve | accept | E-4, Gate 0 |
| Condition D + relay condition + non-inferiority | accept for the later registration | Part 2 "Later" |
| Model-tier routing as the highest-upside experiment | accept, behind Gate 2 | Part 2 "Later" |
| Paper minimum ≥5 devs / ≥200 sessions / ≥1,000 children | adopt as the dataset target | Part 2 "Later" |
| Reframe: "your most expensive agents waste the most on re-discovery" | adopt for positioning once E-1 and F-1 support it | — |
| Named triage owner with stop-ship authority | founder | M-12 |
