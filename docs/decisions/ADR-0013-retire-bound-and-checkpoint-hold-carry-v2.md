# ADR-0013 — Retire bound and checkpoint commands; carry v2 stays off

- **Status:** Accepted 2026-10-31 (overseer, following the Muse audit and the frozen Gate 0 bars; founder may amend)
- **Amends:** ADR-0012 points 1–3
- **Evidence:** `docs/plans/gate-0-results-2026-10-31.md` (E-4, E-7), `docs/bound-coach-replay.md`,
  `docs/deviations/2026-10-30-v2-mcp-race-and-double-count.md`

## Context

ADR-0012 retired fold, said bound was fold's replacement, kept the checkpoint commands working, and named carry v2
as the replacement for checkpoint-restart. Three measurements since then change that:

1. **Bound** saved no tokens in the replay (0 results had a dropped head at the default limit) and, in the one case
   where it did drop output, it dropped an error line. A replacement that has not saved anything is not a
   replacement.
2. **Checkpoint-restart** measured 1.25× the cost of the shipped arm on long sessions after the usage recount. The
   Muse audit asked for retired features to leave the tester surface.
3. **Carry v2** restores 1.23× (3 KB) and 1.08× (4 KB) the dropped edited files of v1 per KB at an equal cap, below
   the 1.5× bar frozen in Gate 0 (E-4). Most of its headline gain came from a larger allowance.
4. **E-7** found full tool output stored under the state directory (fold blobs), which a tester build must not leave
   behind.

## Decision

1. Fold, bound and the checkpoint commands are **retired from the tester surface**: not documented, not in `--help`
   or the tour, and turning them on prints a retired notice. Their code and tests stay in the repository as history
   and for the paper's negative results.
2. The daemon stops writing fold blobs and removes existing ones on first start of the new version.
3. **Carry v1 is the shipped carry.** `carry_v2` stays default off and gets no paid confirmation. Selection must beat
   v1 at equal bytes on a new offline score (and then on testers' logs) before it returns.
4. ADR-0012's statement that carry v2 supersedes checkpoint-restart is withdrawn; nothing currently supersedes it.
   The fidelity measurements (compaction drops 460 of 614 edited files) remain the open problem.

## Consequences

- The tester build has fewer features and a smaller attack surface. Diet, measurement and the opt-in Brief remain.
- Users who relied on `/checkpoint` lose it. It was alpha, and the notice points here.
