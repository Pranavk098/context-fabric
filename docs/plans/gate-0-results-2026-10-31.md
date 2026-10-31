# Gate 0 results: E-1…E-4, E-6, E-7 against the frozen bars (2026-10-31)

Overseer-authored. Bars are the ones in `gate-criteria-2026-10-31.md`, committed before any
output below was read. All numbers: founder machine, tuning set, exploratory, list-price
equivalents. Nothing here goes to the README or paper; the held-out set is testers' logs
(F-1).

| ID | Bar | Result | Verdict |
|---|---|---|---|
| E-1 | Re-discovery ≥ 5% of total session cost | **1.4% (lower) to 2.5% (upper)** of total session cost, 54 sessions, $1,400 total. The parent is 78.5% of session cost. On the old child-cost denominator the same numbers read 6.6%–11.6%. | **FAIL.** Brief is not a product headline. Keep opt-in, no tester claim |
| E-2 | Churn ≥ 3% of 30-day spend and ≥ 15/20 hand-checked | Repeat announcements carry ~4.4M tokens; priced **$0.15 (cache read) to $2.66 (cache write)** against ~$1,400. That is 0.01%–0.2%. 278 flagged sessions, 39% of them headless; the shipped rule flags the same set as the first-announcement-excluded rule. The 20-session hand check was not done because the dollar bar failed first. | **FAIL.** Churn never leads a message. The doctor warning stays as a hygiene note |
| E-3 | A budget with coverage ≥ 50% **and** bytes-weighted precision ≥ 25% | Coverage is 88%–93% at every budget from 2 KB to 20 KB (Opus/Sonnet, parent knew ≥3 files: 89.1% at 2 KB). Bytes-weighted precision is **4.6%–7.1%** at every budget. Packet-files-read precision 11.6%–13.0%. | **FAIL.** Coverage is fine and cheap, precision is the problem and a bigger budget does not fix it: the packet names files the child never opens. No paid Gate 2 for Brief. Next step is selection (offline), not a budget |
| E-4 | v2 ≥ 1.5× v1 dropped edited files restored per KB at equal cap | At 3 KB: 3.45 vs 2.80 per KB (**1.23×**). At 4 KB: 2.82 vs 2.61 (**1.08×**). The headline gap (46.9% vs 33.1%) is mostly the larger allowance (4 KB vs 3 KB). | **FAIL.** `carry_v2` stays off and gets no paid confirmation. Keep v1 |
| E-6 | p95 ≤ 200 ms, p99 ≤ 1 s per sync hook | 1,377 receipts. Worst p95 is PostToolUse 3.1 ms; worst p99 PostToolUse 7.0 ms. PreCompact is a single sample at 10 ms. 0 over the handler timeout, 0 errors. | **PASS** |
| E-7 | Zero stored excerpts, prompts or tool output; ACL denies other users | JSONL logs: 1,430 lines, 0 flagged (content-free). Windows ACL: PASS. **But the state dir holds 7 fold blobs (`obs/`, 299,630 bytes) of full tool output**, plus a carry snapshot with bounded prose. | **FAIL on the strict bar** (stored tool output exists). Fold is retired (ADR-0012), so the blobs are dead weight: M-7 must stop writing them and purge `obs/` on upgrade, and SECURITY.md must say the state dir is user-private, not content-free |

## What this changes

1. **Positioning.** The Muse-suggested line "your most expensive agents waste the most on
   re-discovery" is not supported on this machine. Re-discovery is 1.4%–2.5% of session
   cost. Lead with **diet** (deterministic) and **measurement**. Brief stays an opt-in
   experiment and is not mentioned as a saving.
2. **E-3 detail worth keeping.** Coverage of 92% with precision of 12% means the packet is
   an over-inclusive file list. A selector that predicts which files the child will read
   (for example by task text) is the only path left; it can be tested offline for free.
3. **Carry v2** does not select better. Say so in the deviation/decision log and keep the
   flag off. The "where did selection not improve" question: v2's gain over v1 comes from
   edited-first ordering plus a bigger cap, and once the cap is equal it mostly vanishes.
4. **Added work (M-7 scope):** stop writing `obs/` blobs, purge existing ones on upgrade,
   and drop the "no stored text" language from anything public.
5. **Held-out check.** F-1 will report E-1 and E-3 numbers from testers' logs. The bars do
   not move. If testers' sessions show ≥ 5% re-discovery, that is a real, different
   population, and it is reported as such.

## Direction after review (2026-10-31)

- The Brief selector sprint is **dropped**, not postponed: even a perfect selector can save only about the 1.4%-2.5% of total session cost that re-discovery represents. It returns only if testers' logs show a very different split.
- The parent session carries about 78% of cost, so the next research direction is parent-side: compaction timing and quality, when to start fresh versus continue, and prefix diet at scale.
- Tester round 1 is measurement-only (checkup, diet, fidelity, `handoff-stats --share`), so their logs re-test E-1 independently. Brief is an optional extra.
- Triage owner (M-12): the founder.

## Deviations

(none)
