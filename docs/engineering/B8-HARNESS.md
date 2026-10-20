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
  would exceed `CF_B8_DAILY_CAP` (default 2,200,000), it stops cleanly with HTTP 400
  `daily_cap_reached` and a **"Resume tomorrow"** message — nothing is forwarded, so
  no token is spent over the cap. The check-and-reserve is serialized under a lock (audit
  F11). For the smoke run set `CF_B8_DAILY_CAP=600000`.
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

Then the smoke (1 task × 3 arms, graded). The smoke runs the first task of the **n=30**
draw (`astropy__astropy-7606`), which is **not** in the frozen n=10 sample, and writes every
artifact under a dated `--artifact-root` so it cannot contaminate the v3 paths (only the
shared ledger is touched — it is the money cap):

```bash
cd /mnt/c/dev/Context-Fabric
CF_B8_DAILY_CAP=600000 OPENAI_API_KEY="$OPENAI_API_KEY" \
  ~/.venvs/swe/bin/python -m evals.b8_live \
    --seed 20261019 --n 30 --limit 1 \
    --model gpt-4.1-mini \
    --repo-dir <repo-dir> \
    --artifact-root evals/reports/b8_smoke_v3_<date> \
    --daily-cap 600000 --grade
```

The full pre-registered run is the same command with `--n 10` (the default), `--limit`
removed (all 10 frozen tasks), no `--artifact-root`, and `--daily-cap 2200000`, run across
successive UTC days (`--resume` skips finished task × arm pairs; the ledger and predictions
persist across invocations). Before starting a task × arm the runner requires today's
remaining budget to be at least `--pair-reserve` (default 550,000), otherwise it stops
cleanly for the day.

The runner, per invocation:

1. loads SWE-bench Verified (pinned revision) and draws the seeded repo-stratified sample;
2. produces each task's Parent hand-off once, cached to `evals/parent_handoff_raw/` with
   provenance (model, prompt hash, harness git sha, the BM25-ranked listing shown);
3. spawns `cfx serve` (passthrough + window cap + ledger; `--obs-fold` only for Arm C) and
   runs each arm's sub-agent (`mini-swe-agent` DefaultAgent, `temperature=0.0`,
   `step_limit=35`, `max_tokens=1024`, 10,000-char per-command output cap). Every arm gets
   the same task header (problem statement + sub-agent query + parent notes) before its
   own hand-off;
4. writes predictions in SWE-bench format (`evals/reports/b8_predictions.jsonl`) and, with
   `--grade`, grades them via `swebench.harness.run_evaluation` (`--max_workers 2`);
5. appends one JSONL row per finished/cap-stopped/failed task × arm to
   `evals/reports/b8_runs.jsonl` (crash-safe) and writes a receipt
   `evals/reports/b8_live_<UTC ts>.json`. `evals/b8_aggregate.py` computes per-arm R/O/U
   and McNemar from that JSONL plus the official harness summaries.

## Notes and known limits

* Arms run arm-major (all Arm A, then B, then C) so `cfx serve` can toggle `--obs-fold` per
  arm with one restart. For the 1-task smoke this equals the design's triplet-interleaved
  order; the full 10-task run's triplet interleaving is a future refinement.
* Streaming requests are refused with HTTP 400 `stream_not_ledgered` when the ledger is on
  (a streamed response cannot be charged to the daily cap); the sub-agent's
  `litellm.completion` is non-streaming, so it is unaffected.
* Window-cap input count is a tiktoken `o200k_base` estimate (message content + tool-call
  arguments + the serialized `tools` schema + per-message framing), so it is an upper bound
  on the real prompt rather than an undercount.
* Arm B's compiled packet mounts the shared repo cartridge (`_repo_cartridge`, the Tier-0
  project hologram) exactly as Arm C's repo map does; both B and C open with the hologram,
  and only the code representation below it differs.
