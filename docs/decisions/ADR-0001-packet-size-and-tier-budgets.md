# ADR-0001 — Packet Size and Tier Budgets

- **Status:** Accepted (pending stakeholder review, `CHANGES-FROM-PROPOSAL.md` Q1)
- **Date:** 2026-10-16
- **Supersedes:** contradictory figures in `Arch-Spec.md` §1/§3, `Prod-Intro.md` §2.

## Context

The original documents carried three mutually inconsistent packet sizes: a "3,000-token
window" headline (`Prod-Intro.md`), tier budgets summing to ~8,000 tokens (`Arch-Spec.md`
§3), and an invariant of "≤3,500 dynamic tokens" (`Arch-Spec.md` §1). The packet size is
the most load-bearing number in the product (it determines cost, VRAM, and the "L1
register" claim), so it must have exactly one definition.

## Decision

1. **`hard_total_tokens` is a per-`AgentManifest` parameter.** The v1.0 default is **4,000**.
2. **Tier proportions are normative and scale with the base:** Tier 0 = 15%, Tier 1 = 25%,
   Tier 2 = 5%, Tier 3 = 45%, Reserve = 10%.
3. **At the default base of 4,000:** Tier 0 = 600, Tier 1 = 1,000, Tier 2 = 200, Tier 3 =
   1,800, Reserve = 400.
4. **The "~3,000 dynamic tokens" headline = Tier 1 + Tier 2 + Tier 3** (1,000 + 200 + 1,800).
   Tier 0 is a shared, cache-resident static cartridge; the 400-token reserve is not part of
   the dynamic working set.
5. Any other absolute packet-size figure in the docs is retired. `Module-Implementation.md`
   Stage 0 already lists only percentages and needs no change.

## Consequences

- The two headline numbers (3,000 dynamic / ~3,500 incl. cartridge+reserve) become true at
  once, removing the 2.5× contradiction.
- The default is deliberately lean for the 8 GB-VRAM beachhead; wider manifests can raise
  the base without changing any algorithm.
- Tier token counts must be recomputed from proportions if the base changes — no hardcoded
  absolutes anywhere in code.
