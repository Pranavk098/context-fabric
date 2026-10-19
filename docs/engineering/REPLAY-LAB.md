# Replay Lab — Offline Trajectory Replay (exploratory)

**Status:** `evals/replay_lab.py` + `sidecar/obsfold_v2.py`, offline (no model, no GPU, no
API). Tests: `tests/test_replay_lab.py`, `tests/test_obsfold_v2.py`.

> **EXPLORATORY. Not a result of record.** Replay shows *context shape* — token counts,
> overflow, prefix stability — not task success. A folded trajectory that fits the window
> may still solve nothing, and one that overflows may still solve the task. No accuracy or
> cost headline is claimed here.

## What it does

`python -m evals.replay_lab` loads recorded mini-swe-agent trajectories (`*.traj.json`),
replays every step through three observation-folding policies, and reports the context
shape of each:

* **`none`** — messages verbatim (the `passthrough` arm as run today);
* **`obsfold`** — `sidecar.obsfold.fold_messages` (imported and called as-is, unchanged);
* **`obsfold_v2`** — `sidecar.obsfold_v2.fold_messages` (content-aware, see below).

Per step and policy it reports input tokens (tiktoken `o200k_base` via
`sidecar.obsfold.estimate_tokens`), what was folded, would-overflow at the 24,576 and
16,384 window caps, and the byte-identical leading prefix length vs the previous step (a
cache-hit proxy). It also reports the share of tokens that are tool outputs, and
cross-checks the recorded `prompt_tokens`/`cached_tokens` when the trajectory records them.

Run: `python -m evals.replay_lab --traj-dir <dir>  # scans for *.traj.json` (receipt →
`evals/reports/replay_lab_*.json`).

## Recorded trajectories (read-only inputs)

mini-swe-agent saves one trajectory per (instance, arm) at
`<repo_dir>/<instance_id>/<instance_id>.<arm>.traj.json` (see
`evals/b8_live.py::run_subagent` → `output_path`). The day-1 B8 pilot (and the earlier
astropy smoke) left six under the WSL2 run dir `~/b8_repos` (distro `Ubuntu`, user
`pranav`). They are read, never modified:

| trajectory | steps | WSL2 path (`~/b8_repos/…`) |
| :-- | --: | :-- |
| `astropy__astropy-7606.A` | 30 | `astropy__astropy-7606/astropy__astropy-7606.A.traj.json` |
| `astropy__astropy-7606.B` | 10 | `astropy__astropy-7606/astropy__astropy-7606.B.traj.json` |
| `astropy__astropy-7606.C` | 35 | `astropy__astropy-7606/astropy__astropy-7606.C.traj.json` |
| `django__django-11532.A` | 35 | `django__django-11532/django__django-11532.A.traj.json` |
| `django__django-12262.A` | 35 | `django__django-12262/django__django-12262.A.traj.json` |
| `django__django-13279.A` | 15 | `django__django-13279/django__django-13279.A.traj.json` |

From Windows the same files are `\\wsl.localhost\Ubuntu\home\pranav\b8_repos\…`. New B8
runs add one file per (instance, arm) each day; re-run the lab against the growing dir.

## Results (day-1 pilot + smoke, 2026-10-19)

Receipt: `evals/reports/replay_lab_20261019.json`.

### Tool outputs as a share of tokens

| trajectory | tool tokens / **all** | tool tokens / **turns** |
| :-- | --: | --: |
| astropy A | 59.6% | 99.1% |
| astropy B | 15.2% | 77.6% |
| astropy C | 53.0% | 98.7% |
| django-11532 | 44.9% | 93.9% |
| django-12262 | 34.6% | 83.7% |
| django-13279 | 33.1% | 91.7% |

**Does it match the external 67–84%? Yes — when measured the same way.** The external
figure ("tool outputs are 67–84% of agent tokens") counts the *growing multi-turn
trajectory* (`assistant` + `tool`), excluding the fixed system prompt and handoff. Measured
that way, tool outputs are **77.6–99.1%** of our trajectories. The lower "share of all
tokens" column (15–60%) is the same denominator widened by our fixed ~8k-token
system+user handoff, which is not tool output; it does not contradict the external claim.

### Per trajectory and policy

`total_tokens` is cumulative context tokens (sum of every step's input, Θ(T²) — what is
billed, not the final context length). `overflow@16k` / `overflow@24k` is the first step
whose *estimate* exceeds the cap (`—` = never).

| trajectory | policy | total tokens | peak tokens | overflow@16k | overflow@24k |
| :-- | :-- | --: | --: | --: | --: |
| astropy A (30) | none | 477,761 | 22,679 | 16 | — |
| | obsfold | 339,323 | 12,290 | — | — |
| | obsfold_v2 | 457,235 | 20,940 | 18 | — |
| astropy B (10) | none | 71,518 | 7,728 | — | — |
| | obsfold | 68,589 | 7,455 | — | — |
| | obsfold_v2 | 71,181 | 7,643 | — | — |
| astropy C (35) | none | 514,508 | 19,527 | 21 | — |
| | obsfold | 377,774 | 11,621 | — | — |
| | obsfold_v2 | 497,844 | 18,416 | 23 | — |
| django-11532 (35) | none | 460,701 | 17,279 | 31 | — |
| | obsfold | 372,751 | 11,991 | — | — |
| | obsfold_v2 | 439,144 | 16,408 | 34 | — |
| django-12262 (35) | none | 415,142 | 15,533 | — | — |
| | obsfold | 365,567 | 13,408 | — | — |
| | obsfold_v2 | 397,337 | 14,467 | — | — |
| django-13279 (15) | none | 180,584 | 13,934 | — | — |
| | obsfold | 160,262 | 11,479 | — | — |
| | obsfold_v2 | 178,976 | 13,613 | — | — |

### Prefix stability (cache-hit proxy)

Byte-identical leading prefix length between consecutive steps, using canonical message
bytes as the prompt proxy. The fixed system+user handoff is always preserved (the
minimum common prefix is identical across policies within each trajectory); the *average*
shared prefix differs:

| trajectory | none | obsfold | obsfold_v2 |
| :-- | --: | --: | --: |
| astropy A | 124,476 | 95,391 | 113,111 |
| astropy B | 38,704 | 34,330 | 35,269 |
| astropy C | 113,520 | 90,368 | 105,541 |
| django-11532 | 98,569 | 83,253 | 91,580 |
| django-12262 | 93,889 | 83,614 | 87,813 |
| django-13279 | 79,987 | 68,531 | 74,250 |

`none` is strictly append-only (best). Both folding policies reduce the average shared
prefix because the fold boundary advances every `k=3` observations and rewrites earlier
messages from verbatim → stub — the known ReFold cache-alignment tradeoff. `obsfold_v2`
keeps more content per folded message, so it sits between `none` and `obsfold`.

## Known limits

* **The tiktoken estimate undercounts the billed tokens.** `estimate_tokens` counts
  content text + 4 framing tokens/message and omits chat-template / tool-call
  serialization overhead. astropy A's recorded `prompt_tokens` peaked at **23,493** at
  step 29 (cached 22,912), and the run overflowed on its 31st call at **24,730** total
  (23,706 in messages + 1,024 completion) — while the estimate peaked at 22,679. So
  "would-overflow@24k = —" from the estimate is ~3–4% conservative; the recorded
  `prompt_tokens` are the truth for the real run.
* **`obsfold_v2` is content-preserving but position-lossy.** Kept lines stay verbatim and
  in order; the removed remainder is collapsed to one stub. Whole-content ("other") folds
  are exactly reversible via `sidecar.obsfold.expand`.
* **Classification is heuristic** (content patterns for diff/test, line count for long
  files). No command/`pytest`/`git diff` signal is used, so an unusual output can be
  misclassified; determinism is unaffected.

## Determinism

No clock, no RNG, no model. `fold_content` / `fold_messages` are pure functions of the
content and task identifiers; replay is a pure function of the message list. Stub ids are
content-addressed (same `make_stub` as `sidecar/obsfold`), and `seen_hash` uses RFC 8785
canonicalization.
