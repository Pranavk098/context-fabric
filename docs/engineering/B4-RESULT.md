# B4 Result — divergent-role compiled context vs prefix-cache-only: **KILL**

Pre-registration: `docs/engineering/B4-PREREGISTRATION-v2.md` (frozen 2026-10-18 21:28 UTC).
Result of record: `evals/reports/b4_divergent_20261018_145937.json` (iteration 1, §11 of the
pre-registration: `--max-model-len 16384`, nothing else changed). The superseded first run,
`evals/reports/b4_divergent_20261018_145719.json`, lost 9/36 arm-B requests to context-length
rejections and is kept for the audit trail only.

Model: Qwen2.5-1.5B-Instruct, vLLM with prefix caching, RTX 5070 Laptop 8 GB, `temperature=0`,
`max_tokens=48`. n = 12 objectives × 3 repeats = 36 requests per arm.

## Numbers

| arm | quality | billed prompt tokens | prefix-cache hits | uncached prompt tokens | peak KV |
|---|---|---:|---:|---:|---:|
| A broadcast | 35/36 (0.97) | 292,314 | 283,584 | 8,730 | 6.5% |
| B prefix-cache-only (baseline) | 29/36 (0.81) | 293,079 | 284,208 | 8,871 | 6.6% |
| C compile (mechanical fabric) | 21/36 (0.58) | 41,172 | 27,312 | 13,860 | 1.7% |

## Verdict against the frozen criteria

- Quality: `C.passes (21) >= B.passes (29) − 4` → **false**.
- Uncached tokens: `C (13,860) <= B (8,871)` → **false**.
- → **KILL**: a real quality regression beyond tolerance. Compiled context also loses on
  uncached prefill.

## What this does and does not mean

- **Prefix caching already captures the token win here.** When every role shares one raw
  prefix, vLLM caches it once and later requests pay only their short tails. The compiled arm
  gives each role a different packet, so each distinct packet pays a cold prefill. It sends
  7× fewer *billed* tokens and holds about ¼ of the peak KV, but it costs **1.56× more uncached
  tokens**, which is the real prefill cost. Billed-token ratios against a prefix-cached baseline
  are not a valid headline.
- **Mechanical memory costs quality on a 1.5B model.** On the same task broadcast scored 35/36,
  prefix-cache-only 29/36 and compile 21/36.
- **Scope of the result:** one repository slice (~8k tokens, which fits in context), one small
  model, 12 objectives. It says nothing about contexts that do not fit, much larger fleets, or
  larger models. It does falsify "compiled per-role context beats prefix caching" for
  fits-in-context divergent-role tasks at this scale.

## Per-objective passes (out of 3)

| objective | A | B | C |
|---|:-:|:-:|:-:|
| planner-1 | 3 | 3 | 0 |
| planner-2 | 3 | 3 | 0 |
| planner-3 | 3 | 3 | 3 |
| implementer-1 | 3 | 3 | 3 |
| implementer-2 | 3 | 0 | 0 |
| implementer-3 | 3 | 3 | 0 |
| tester-1 | 3 | 3 | 3 |
| tester-2 | 3 | 1 | 3 |
| tester-3 | 3 | 3 | 0 |
| reviewer-1 | 2 | 1 | 3 |
| reviewer-2 | 3 | 3 | 3 |
| reviewer-3 | 3 | 3 | 3 |

## Post-hoc observations (diagnosis only; they do not change the verdict)

- **Truncation:** in two failures (planner-1, implementer-3) the compile-arm answer was verbose
  and hit `max_tokens=48` before it named the answer.
- **Wrong picks:** planner-2 answered `_client, _types`, tester-3 answered `is_server_error`,
  implementer-2 answered `class, NetRCAuth`.
- **Pre-registration/code drift:** §3 says imports are "deliberately not a block kind", but
  `evals/mech_fabric.py` emits a per-file imports block. The planner's import facts *were* in
  the packet. That makes the planner failures a model/format failure, not a recall gap. The
  doc statement was wrong, and it is recorded here rather than edited after the run.
- A new experiment that changes the fabric, the packet format or `max_tokens` needs its own,
  separately dated pre-registration (§8). It is not a re-run of this one.
