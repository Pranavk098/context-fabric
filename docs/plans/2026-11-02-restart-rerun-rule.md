# Restart break-even re-run: rule written before it runs (2026-11-02)

Status: **rule only. Nothing has been run under it.** The commit that adds this note is cited by the results.

## Why

`docs/plans/2026-11-02-p4-results.md` M3 reported 40.9% (18 of 44 long sessions) with positive
restart headroom. That figure assumes the handoff packet loses nothing. P4 M3 says a break-even that
needs a complete packet does not count. So 40.9% is a ceiling. This note fixes how an incomplete
packet is charged so the number that counts is not chosen after seeing it.

## Rule

1. **Handoff quality** is set to carry v2's measured recall: **46.9% of the dropped working set kept**
   (`docs/carry-fidelity-score.md`, 245 of 522 dropped edited files restored).
2. **What the handoff misses (53.1%) is added back on the restart side.** At a restart at turn `t`, the
   context gathered before the restart is `prompt(t-1) - prefix` tokens. The missed share,
   `(1 - 0.469) x` that amount, has to be gathered again.
3. **Price of the re-gather:** the cost of putting that context in originally, i.e. as **new tokens at
   the model's cache-write (5m) rate**, the same rate the script already charges the opening prefix.
   It is not priced as a cache read.
4. The re-gathered tokens then **join the restarted context**, so every later restarted turn also pays
   to read them at the cache-read rate.
5. **Pessimistic case:** the same calculation at carry v1's measured recall, **33.1%** (173 of 522).
   Reported next to the main case, never instead of it.
6. **Bar unchanged** (D-2): at least **25%** of 40+ turn sessions with a break-even turn that has
   strictly positive headroom. The bar is judged on the 46.9% case. The 33.1% case is context.
7. Packet size stays at the existing default (3 KB, 768 tokens). Nothing else in the model changes.

## What this can and cannot say

- Still modelled, still one machine, still not a saving claim, and nothing in the plugin restarts a
  session.
- Recall of edited files is a proxy for how much of the working set a handoff keeps. Using it for all
  gathered context is a simplification; it is stated, not hidden.
- Either outcome is reported: if the 46.9% case falls under 25%, D-2 is not cleared and P7 stays
  unbuilt.
