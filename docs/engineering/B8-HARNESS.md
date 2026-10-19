# B8 Live Test Harness — setup, versions, runbook, money safety

Pre-registered design: [`B8-TEST-DESIGN.md`](B8-TEST-DESIGN.md). This harness runs the
paired sub-agent evaluation: one frozen Parent localization trace per SWE-bench Verified
task, then three arms (A naive dump / B current CF packet / C BM25 chunks + repo map +
`--obs-fold`), each a `mini-swe-agent` sub-agent routed through `cfx serve` under a hard
24,576-token window cap, graded by `swebench.harness.run_evaluation` in Docker.

## Components

| Component | File | What it does |
|---|---|---|
| Sidecar window cap | `sidecar/b8.py`, `sidecar/server.py`, `sidecar/cli.py` | `--window-cap N` returns HTTP 400 `context_length_exceeded` when rendered input + `max_tokens` exceeds N (tiktoken `o200k_base`); off by default, byte-identical when off. |
| Money safety | `sidecar/b8.py`, `sidecar/server.py` | Model allow-list (`gpt-5.4-mini`, `gpt-4.1-mini`, `gpt-4o-mini`) + persistent daily token ledger (JSONL under `evals/reports/b8_ledger/`, one file per UTC day) enforced at the `cfx serve` choke point before any upstream spend. |
| Runner | `evals/b8_live.py` | Seeded repo-stratified sampling, frozen Parent hand-off cache, arm hand-off builders, sub-agent driver, predictions, grading, receipt. |
| Tests | `tests/test_b8.py` | Allow-list, daily-cap stop/resume, window-cap error shape, sampler determinism, resume skip. No network. |

## Setup (WSL2 Ubuntu + Docker Desktop)

Create a dedicated venv (do not touch `~/.venvs/vllm` or the repo's `uv` env):

```bash
# in WSL2 Ubuntu
uv venv --clear --python 3.12 ~/.venvs/swe
uv pip install --python ~/.venvs/swe/bin/python "mini-swe-agent" "swebench" "mcp"
uv pip install --python ~/.venvs/swe/bin/python -e /mnt/c/dev/Context-Fabric
```

The runner spawns `cfx serve` itself (`python -m sidecar serve …`), so the sidecar runs in
the same venv as the runner and the sub-agent. Docker must be running (the sub-agent and
the grader both use the `swebench/sweb.eval.x86_64.<instance_id>` images).

### Recorded versions (2026-10-19)

| Package | Version |
|---|---|
| Python (venv) | 3.12.14 |
| mini-swe-agent | 2.4.6 |
| swebench | 5.0.2 |
| datasets | 5.1.0 |
| litellm | 1.106.0 |
| mcp | 2.3.0 |
| tiktoken | 0.14.0 |
| Docker | 29.0.1 |

## Money safety (hard requirements)

* **Allow-list.** Only `gpt-5.4-mini`, `gpt-4.1-mini`, `gpt-4o-mini`. The runner refuses any
  other model name before launching, and the sidecar refuses a non-allow-listed model with
  HTTP 400 `model_not_allowed` (defence in depth at the single choke point). An
  `openai/` provider prefix is accepted.
* **Daily ledger.** Every upstream API response's usage is appended to
  `evals/reports/b8_ledger/tokens-<UTC-day>.jsonl` (`prompt_tokens`, `completion_tokens`,
  `reasoning_tokens`, `cached_tokens`, `uncached_tokens`, `total_tokens`).
* **Daily cap.** Before each request the sidecar sums today's ledger; if that total plus the
  request's maximum possible tokens (rendered input estimate + `max_completion_tokens`)
  would exceed `CF_B8_DAILY_CAP` (default 2,200,000), it stops cleanly with HTTP 429
  `daily_token_cap_exceeded` and a **"Resume tomorrow"** message — nothing is forwarded, so
  no token is spent over the cap. For the smoke run set `CF_B8_DAILY_CAP=600000`.
* **Choke point.** All agent traffic (Parent localization + every sub-agent turn) goes
  through `cfx serve`, upstream `https://api.openai.com`. `OPENAI_API_KEY` is read from the
  environment (passed into WSL via `WSLENV` or set at call time); it is never printed or
  logged.

## How to run

First extract the repos for the tasks you will run (the runner expects one directory per
`instance_id` under `--repo-dir`, e.g. `<repo-dir>/<instance_id>/` with the source at the
task's base commit):

```bash
# example for a single instance_id (image name is instance["image_name"] if present)
docker run --rm <image> tar -c -C /testbed . | tar -x -C <repo-dir>/<instance_id>
```

Then the smoke (1 task × 3 arms, graded):

```bash
cd /mnt/c/dev/Context-Fabric
CF_B8_DAILY_CAP=600000 OPENAI_API_KEY="$OPENAI_API_KEY" \
  ~/.venvs/swe/bin/python -m evals.b8_live \
    --seed 20261019 --n 30 --limit 1 \
    --model gpt-4.1-mini \
    --repo-dir <repo-dir> \
    --daily-cap 600000 --grade
```

The full pre-registered run is the same command with `--limit` removed (all 30 tasks) and
`--daily-cap 2200000`, run across successive UTC days (`--resume` skips finished
task × arm pairs; the ledger and predictions persist across invocations).

The runner, per invocation:

1. loads SWE-bench Verified and draws the seeded repo-stratified sample;
2. produces each task's Parent hand-off once, cached to `evals/parent_handoff_raw/`;
3. spawns `cfx serve` (passthrough + window cap + ledger; `--obs-fold` only for Arm C) and
   runs each arm's sub-agent (`mini-swe-agent` DefaultAgent, `temperature=0.0`,
   `step_limit=35`, `max_tokens=1024`, 10,000-char per-command output cap);
4. writes predictions in SWE-bench format (`evals/reports/b8_predictions.jsonl`) and, with
   `--grade`, grades them via `swebench.harness.run_evaluation` (`--max_workers 2`);
5. writes a receipt `evals/reports/b8_smoke_<ts>.json` with per-arm
   tokens (billed/cached/uncached), overflow flag, steps, resolved flag, wall time, the
   ledger total, and the projected tokens for 30 tasks × 3 arms at 2.2M tokens/day.

## Notes and known limits

* Arms run arm-major (all Arm A, then B, then C) so `cfx serve` can toggle `--obs-fold` per
  arm with one restart. For the 1-task smoke this equals the design's triplet-interleaved
  order; the full 30-task run's triplet interleaving is a future refinement.
* Streaming responses are not ledgered (mini-swe-agent's `litellm.completion` is
  non-streaming by default); only the non-streaming path records usage.
* Window-cap input count is a tiktoken `o200k_base` estimate (message content + tool-call
  arguments + 4 framing tokens/message); it ignores the constant `tools` schema overhead,
  which is equal across arms and does not bias the comparison.
