# Roadmap Decisions — Deliberate Deferrals & Triggers (2026-10-18)

Companion to `IMPLEMENTATION-STATUS.md` (what is built). This file records what we chose **not**
to build yet, why, and the trigger that un-defers it. Last reviewed: 2026-10-18.

| # | Gap | Decision | Rationale | Trigger to act |
| :--- | :--- | :--- | :--- | :--- |
| 1 | `agent_hint` → engine (vLLM/LMCache wiring) | Thin slice shipped (`kernel/router/hints.py`, trace `cache_hints`); sending deferred | vLLM RFC #52113 is **open, not shipped**; emitting against an unmerged interface is speculative | RFC merges, or an LMCache connector sprint starts |
| 2 | LLM-in-the-loop summarization | Not in the compile path | Determinism/auditability is the product: LLM summaries are nondeterministic and unverifiable; the compression trade-off is accepted and measured | A user-visible quality gap on real prose that extractive rendering cannot close, with a deterministic fallback design |
| 3 | Multi-worker / production concurrency | Deferred | `ThreadingHTTPServer` is fine for the local single-operator beachhead (compile overhead is ms-scale); multi-tenant is not a use case yet | First hosted/team deployment, or a >100 req/s single-box requirement |
| 4 | Postgres store | Deferred (SQLite v0 shipped) | Research: minimal persistence belongs in v0; SQLite covers the beachhead; Postgres earns its weight at team/hosted scale | Team/hosted deployment, or cross-store joins required |
| 5 | OpenTelemetry wiring | Optional extra exists, unused | No operator asking for spans yet; local debugging is served by `/fabric/stats` + demo receipts | First hosted deployment, or an operator request |
| 6 | Full U-curve head/tail scheduling | Partial (tail recap + anchor shipped) | The position probe (n=8, 1.5B) was a **null** — no measured benefit justifies more placement machinery | A measured position effect on a realistic model/context, or user-reported constraint-following failures |

**Rule:** none of these are "corners cut" — each is a measured, documented trade-off with an
explicit trigger. When a trigger fires, open a scrum item and reference this file.
