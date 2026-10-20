# Terminal-Bench 2.1 Harness Prep & Adapter Design (Track T1)

- **Status:** Design + offline adapter skeleton (no live runs, no API spend, no Docker work yet).
- **Date:** 2026-10-20
- **Predecessors:** `docs/engineering/B8-TEST-DESIGN.md`, `docs/engineering/B8-HARNESS.md`, `docs/research/SUBAGENT-HANDOFF-LANDSCAPE.md` Part 2 (§2.4 recommends Terminal-Bench 2.1 as the next benchmark after B8).
- **Problem statement under test (unchanged from B8):** *"A sub-agent spawned on a small context window succeeds more often, at lower cost, with Context Fabric than without."* Terminal-Bench 2.1 extends this from single-file Python repair to hard, polyglot, long-horizon CLI tasks where context caps bind far harder.

---

## 0. Executive Summary Box

```text
1. Harness      : Harbor (`harbor` CLI, `pip install harbor`) — the official TB 2.0/2.1 runner;
                  the legacy `tb` CLI is only for Terminal-Bench 1.0 / beta.
2. Dataset      : terminal-bench/terminal-bench-2-1 on Harbor Hub — 89 tasks, 0 unsolved.
3. Scaffold     : Harbor's pre-integrated `mini-swe-agent` (same scaffold as B8), model calls
                  routed through `cfx serve` via `--agent-env OPENAI_BASE_URL=http://host.docker.internal:8080/v1`
                  (Harbor 0.24.0 resolves the endpoint from env, not the newer `--base-url` flag).
4. Sandbox      : local `docker` (default). Task images are prebuilt
                  (docker.io/alexgshaw/<task>:20251031); no local build.
5. Window cap   : W_sub = 24,576 tokens in `cfx serve` (identical to B8); hand-off slot 8,192.
6. Arms         : A naive raw dump / B current CF packet / C BM25 line-window skim + --obs-fold
                  (1:1 map from B8; B/C are language-agnostic twins — see §3).
7. Sample       : n=10 category-stratified pilot (seed 20261019) → n=50 full (ReFold-aligned).
8. Budget       : exploratory; see §5 (B8 receipts + ReFold/SWE-Pruner + per-task timeouts).
```

---

## 1. Harness facts (all URLs opened 2026-10-20)

### 1.1 The harness is Harbor, not `tb`

Terminal-Bench 2.1 is **not** run by the original `tb` CLI. The `laude-institute/terminal-bench`
README (accessed 2026-10-20) still documents `tb`, but it now points new users to **Harbor**
(`https://github.com/laude-institute/harbor`) as the framework that runs Terminal-Bench 2.0. The
`harbor-framework/terminal-bench-2-1` dataset repo README (accessed 2026-10-20) makes this explicit:
the only supported path is `harbor run -d terminal-bench/terminal-bench-2-1 ...`.

* Install: `uv tool install harbor` (or `pip install harbor`). Cloud sandboxes are optional extras
  (`harbor[daytona]`, etc.); the local **`docker` sandbox is the default and needs no extra**.
* Sources: `https://docs.harborframework.com/getting-started/installation.md`,
  `https://docs.harborframework.com/sandboxes/pre-integrated-sandboxes.md`,
  `https://raw.githubusercontent.com/laude-institute/terminal-bench/main/README.md`,
  `https://raw.githubusercontent.com/harbor-framework/terminal-bench-2-1/main/README.md`.

### 1.2 Task format

A Harbor task is a directory (`https://docs.harborframework.com/tasks/overview.md`):

```text
my-task/
├── instruction.md          # English task instruction (the "problem statement")
├── task.toml               # config, metadata, resources, timeouts
├── environment/
│   ├── Dockerfile          # or [environment].docker_image (prebuilt) — can be omitted
├── solution/
│   └── solve.sh            # oracle/reference solution (NOT given to the agent)
└── tests/
    └── test.sh             # verifier: writes /logs/verifier/reward.txt (0|1) or reward.json
```

* The **verifier** (`tests/test.sh`) runs after the agent and writes a numerical reward to
  `/logs/verifier/reward.txt` (single number) or `/logs/verifier/reward.json` (labeled metrics).
  This is Terminal-Bench's equivalent of SWE-bench's `FAIL_TO_PASS`/`PASS_TO_PASS` grading.
* `task.toml` carries the resources and timeouts: `[environment] cpus / memory_mb / storage_mb /
  gpus / docker_image`, `[agent] timeout_sec`, `[verifier] timeout_sec`,
  `[environment] build_timeout_sec`. Sources:
  `https://docs.harborframework.com/tasks/configuration.md`,
  `https://docs.harborframework.com/tasks/environment.md`,
  `https://docs.harborframework.com/tasks/verifier.md`.

### 1.3 How an agent is plugged in (and how model calls reach `cfx serve`)

Two ways (`https://docs.harborframework.com/agents/custom-agents.md`,
`https://docs.harborframework.com/agents/pre-integrated-agents.md`):

1. **Pre-integrated agent** — `mini-swe-agent` is one of ~40 built-in agents. This is the path we
   use: it is the same scaffold B8 drives directly, so the arms stay comparable.
2. **Custom agent** — subclass `BaseInstalledAgent` (agent CLI runs inside the task container) or
   `BaseAgent` (loop runs in Harbor and drives `BaseEnvironment`). The escape hatch if Harbor's
   `mini-swe-agent` integration cannot be pointed at `cfx serve` non-streamingly.

**Model routing is exactly B8's choke point, via Harbor's model-connection resolution.**

The docs' `--base-url` / `--api-format` flags (`agent-auth-resolution.md`) belong to a **newer**
Harbor than the stable release. **Harbor v0.24.0** (installed 2026-10-20 into `~/.venvs/tbench`)
resolves the endpoint and credential from **environment variables** instead — verified by reading
the installed `harbor/agents/model_connection.py` and `harbor/agents/installed/mini_swe_agent.py`:

* For an `openai/<model>` slug, the provider reads `OPENAI_API_KEY` and `OPENAI_BASE_URL` /
  `OPENAI_API_BASE` (default endpoint `https://api.openai.com/v1`).
* mini-swe-agent adds `MSWEA_API_KEY` (`passthrough=True`) and, when `OPENAI_BASE_URL` is set,
  mirrors it into both aliases before the container launches.
* mini-swe-agent's default model class is `litellm` (chat-completions, the same non-streaming
  `litellm.completion` path B8's ledger binds to); it only switches to the Responses API
  (`model.model_class=litellm_response`) when `reasoning_effort` is set for an openai model.

So the per-arm invocation pins the endpoint via `--agent-env OPENAI_BASE_URL=<cfx>` and passes the
key (`MSWEA_API_KEY` or `OPENAI_API_KEY`) at call time — the adapter's `harbor_command()` builds
this argv (unit-tested offline).

### 1.4 Docker, disk, per-task time, and WSL2 + Docker Desktop

* **Docker requirement:** local `docker` is the default sandbox; the Docker daemon/CLI must be
  installed (same as B8's SWE-bench grader). Task images are **prebuilt** on Docker Hub
  (`docker.io/alexgshaw/<task>:20251031`), so no local image build — only pulls.
* **Disk:** the container **storage budget** is `storage_mb = 10240` (10 GB) per task in
  `task.toml`; the actual **image pull size** is not documented and not verified — several tasks
  (`torch-tensor-parallelism`, `caffe-cifar-10`, `compile-compcert`, `make-doom-for-mips`) bundle
  large toolchains, so budget **5–15 GB per image × 50 tasks (exploratory, not verified)** of
  Docker cache. A single arm pulls its image once; A/B/C reuse the same image.
* **Per-task time:** `[agent].timeout_sec` is **900–2400 s** (15–40 min) and `[verifier].timeout_sec`
  **900–2400 s** in the task manifests I opened (`compile-compcert` 2400 s, `make-doom-for-mips`
  900 s, `torch-tensor-parallelism` 900 s, `crack-7z-hash` agent 1800 s, `caffe-cifar-10` 1200 s).
  Human time estimates in the same manifests span **5–480 min (expert) / 10–1920 min (junior)**.
  `[environment].build_timeout_sec = 600` everywhere (moot with prebuilt images).
* **Resources:** every task I opened requests `cpus 1–2`, `memory_mb 2048–4096`, `gpus 0`,
  `allow_internet true`.
* **WSL2 + Docker Desktop:** **yes.** Harbor's default `docker` sandbox runs through the same
  Docker Desktop + WSL2 engine B8's SWE-bench grader already uses (verified state in
  `docs/engineering/B8-TEST-DESIGN.md` §1.4: Docker Desktop CLI v29.0.1, `Ubuntu` and
  `docker-desktop` WSL2 distros). One WSL2-specific caveat: the agent container reaches the
  host `cfx serve` at `http://host.docker.internal:8080/v1` (Docker Desktop injects that alias);
  on a native Linux Docker you must pass `--add-host host.docker.internal:host-gateway` or use the
  host LAN IP instead. The Harbor auth doc uses exactly `http://host.docker.internal:8000/v1` for
  its local-vLLM example, so this pattern is supported.

---

## 2. Task sampling plan

Terminal-Bench 2.1 has **89 tasks, 0 unsolved** (28 tasks fixed vs. 2.0). The task manifests carry a
`category` (`system-administration`, `software-engineering`, `machine-learning`, `security`, …), a
`difficulty`, and the timeouts above. There is no `repo` dimension, so B8's repo-stratification is
replaced by **category-stratification**:

1. **Canonical sort** by `task_id`.
2. **Stratify by `category`** (the `[metadata] category` field), then allocate seats with
   Hamilton largest-remainder and a floor of ≥1 per category — **reusing B8's frozen
   `sample_instances` verbatim** by relabelling `task_id -> instance_id`, `category -> repo`.
3. **Seeded draw** `seed = 20261019` (same as B8), `random.Random(seed).sample` within stratum.
4. **Freeze a manifest** (`evals/reports/tbench_tasks_<n>.json`) with a SHA-256 digest before the
   first run, mirroring B8's `assert_frozen_sample` drift guard.

* **Pilot:** `n = 10` (default), the B8-style first-day smoke.
* **Full run:** `n = 50`, the ReFold-aligned stratified subset
  (`docs/research/SUBAGENT-HANDOFF-LANDSCAPE.md` §2.2, arXiv:2610.07863 evaluated 50 stratified
  Terminal-Bench 2.1 tasks). The 89-task full suite is the optional final sweep.

The sampler is `evals.tbench.sample_tasks` (unit-tested: determinism, floor coverage, n-respect,
empty input).

---

## 3. Experimental arms (1:1 map from B8, polyglot)

The sub-agent is Harbor's `mini-swe-agent` inside the task container; every model turn passes
through `cfx serve` under `W_sub = 24,576` tokens. The **frozen Parent** (Phase 1) inspects the
task container's filesystem (deterministic `find` + read of top files, plus one mini-model
localization turn at `temperature=0`), producing `candidate_files` + `subagent_query` +
`parent_notes` — cached once per task, so arms share byte-identical parent context.

| Feature | Arm A: naive | Arm B: current CF packet | Arm C: new CF (skim + fold) |
|---|---|---|---|
| **Hand-off** (8,192 tok) | Raw file dump, FIFO-truncated (`handoff_arm_a`, frozen from B8) | `compile_packet` 4-tier over polyglot blocks + shared hologram | BM25 line-window skim (line numbers) + shared hologram |
| **Trajectory** | passthrough | passthrough | `--obs-fold` (reversible `ctx://obs/<id>`) |
| **Prefix cache** | high (append-only) | high (append-only) | high (fold in k=3 chunks, recency kept) |
| **Overflow** | aborts `context_length_exceeded` | aborts `context_length_exceeded` | folds oldest obs first, flat prompt |

**Why B/C are new language-agnostic twins, not B8's builders reused verbatim.** B8's
`evals.b8_live.handoff_arm_b` / `handoff_arm_c` call `evals.mech_fabric.build_blocks` /
`evals.b4_v3.build_ast_chunks`, both stdlib-`ast` (Python-only). Terminal-Bench tasks are polyglot
(C, Rust, Makefile, TeX, CUDA, Spark SQL, …), and `build_blocks` raises `SyntaxError` on non-Python
input. So `evals/tbench/adapter.py` ships:

* `handoff_arm_b` — `_polyglot_blocks`: Python files get symbol facts via `extract_source`
  (which gracefully ignores non-Python), non-Python files degrade to a raw full-file fact that the
  4-tier budget trims. This is the *honest* "current CF" behaviour on a polyglot container.
* `handoff_arm_c` — `_line_window_chunks`: 40-line sliding windows (10-line overlap) with exact
  `L<start>-L<end>` line numbers, BM25-ranked against the instruction, packed with the shared
  Tier-0 hologram — the polyglot twin of B8's question-guided skim.
* `handoff_arm_a` — **reused frozen from B8** (`evals.b8_live.handoff_arm_a` is pure text and
  already language-agnostic).

The shared task header (`build_task_header`) is also reused frozen from B8, with `instruction.md`
as the `problem_statement`. All three arms share it byte-for-byte (unit-tested).

**Per-arm `cfx serve` argv** (`sidecar_command`) mirrors `b8_live._sidecar_command`: `--mode
passthrough --window-cap 24576 --b8-ledger-dir … --b8-daily-cap …`, plus `--obs-fold --obs-store …`
**only for Arm C**. A/B stay byte-identical passthrough under the same window cap and daily ledger.

---

## 4. Metrics (unchanged from B8, re-keyed to Terminal-Bench)

1. **Primary outcome — resolve rate** (`resolved ∈ {0,1}`), the verifier's `reward.txt == 1`
   (`reward.json` preferred if present).
2. **Window-overflow rate** (`overflowed`) — fraction of tasks ending `context_length_exceeded`,
   plus **peak input tokens** per trajectory (recompute the overflow curve at any threshold).
3. **Token volume** from the ledger: `prompt_tokens`, `cached_tokens`, `uncached_tokens`,
   `completion_tokens`, `total_tokens`.
4. **Efficiency** — `tokens_per_resolved` (total tokens / resolved count).
5. **Dollar cost** — `docs/engineering/COSTS.md` presets (`openai:gpt-4o-mini` etc.), same formula.
6. **Diagnostics** — turns-to-first-edit, target-file re-read rate, `fold_events`/`folded_bytes`
   (already surfaced by `cfx serve` `/fabric/stats`, per B8 audit F7).

**Stats:** paired design, exact McNemar on resolve rate + Wilcoxon on tokens/cost (B8 §6, carried
over). PASS gate: Arm C ≥ Arm A + Δ resolves, OR ≥ resolve rate with ≥25% uncached-token reduction
and ≥50% fewer overflows.

---

## 5. Budget per task (exploratory, citable only to opened sources)

Terminal-Bench tasks are long-horizon relative to SWE-bench Verified (single-file Python), so the
per-task token estimate is **higher** and marked *exploratory* until a live pilot.

| Source | Number | Status |
|---|---|---|
| B8 smoke v3 receipt, `evals/reports/b8_smoke_v3_20261020/reports/b8_runs.jsonl` | arm A 61.1k / B 43.9k / C 62.5k `prompt_tokens` over 6 steps (all `LimitsExceeded`, truncated) | **measured**, but truncated — not representative |
| B8-TEST-DESIGN §5.3 | ~220k tokens/arm A/B, ~140k arm C, ~15k parent → ~595k/task-triplet | pre-registered projection, *not verified* |
| B8-HARNESS | a finished task × arm run costs 153k–503k tokens | measured receipt range |
| SWE-Pruner (arXiv:2601.16746) | unbounded `mini-swe-agent` on SWE-bench Verified ~0.79–0.91M tokens/task | literature, SWE-bench (shorter tasks) |
| ReFold (arXiv:2610.07863) | TB 2.1 on local vLLM `$0`; hosted API **~$8–$35 per 50-task arm** on Qwen3.6 / GPT-5.4-mini | literature (via `SUBAGENT-HANDOFF-LANDSCAPE.md` §2.2) |

**Exploratory per-task estimate for Terminal-Bench 2.1** (bounded at 24,576, 35-step limit, larger
polyglot working sets than SWE-bench): **300k–800k tokens / task / arm**; a full n=50 × 3-arm run is
**~45–120M tokens** — 5–14 days at the 2.5M/day mini pool, or 1–2 days at 10M/day. The 10-task
pilot (×3 arms) is **~9–24M tokens**. These are placeholders to be replaced by the pilot's receipt;
they are **not** a citable claim (a separately dated pre-registration must precede any citable
number).

---

## 6. Adapter skeleton (what exists now)

`evals/tbench/` is a new package (nothing frozen touched; no new dependencies):

* `evals/tbench/adapter.py` — pure, offline functions: `sample_tasks`, `handoff_arm_a/b/c`,
  `build_handoff`, `build_task_header` (re-export), `sidecar_command`, `harbor_command`.
* `tests/test_tbench.py` — 11 offline tests (mock model = in-process HTTP upstream; mock harness =
  argv/config builders). Covers sampler determinism + category stratification, arm header-sharing +
  divergence, polyglot tolerance of B/C, line-numbered skim windows, per-arm sidecar argv
  (`--obs-fold` only C), harbor argv (`--base-url` + `--api-format openai_chat_completions` +
  `-a mini-swe-agent`), and the model-routes-through-`cfx serve` contract (window cap + ledger).

**Not yet built** (post-B8 live driver, needs WSL2 + Docker + Harbor + a mini-tier key):
the `main()` that (1) spawns `cfx serve` per arm, (2) runs the frozen Parent over the task
container, (3) runs `harbor run` per arm, (4) collects `reward.txt` + ledger and writes a receipt.
The exact commands are in §7.

---

## 7. Install & run commands (for later, outside the 17:30–23:30 window)

```bash
# WSL2 Ubuntu — a dedicated venv, never touching ~/.venvs/vllm or the repo uv env
uv venv --clear --python 3.12 ~/.venvs/tbench
uv pip install --python ~/.venvs/tbench/bin/python harbor

# no-model sanity check (no Docker, no API, no Hub auth) — RAN 2026-10-20:
~/.venvs/tbench/bin/harbor --version        # 0.24.0
~/.venvs/tbench/bin/harbor agent list       # mini-swe-agent is pre-integrated
~/.venvs/tbench/bin/harbor agent schema mini-swe-agent   # its kwargs (config, litellm_model_registry, max_tokens)

# one-arm smoke (needs Docker + a key + Harbor Hub auth for the dataset). Harbor 0.24.0
# routes the endpoint via env vars, not --base-url:
MSWEA_API_KEY="$OPENAI_API_KEY" ~/.venvs/tbench/bin/harbor run \
  -d terminal-bench/terminal-bench-2-1 \
  -a mini-swe-agent -m openai/gpt-4.1-mini \
  --ae OPENAI_BASE_URL=http://host.docker.internal:8080/v1 \
  -e docker -n 2 -k 1
```

`cfx serve` runs on the host (WSL2) and is spawned exactly as B8 (`sidecar_command`), one instance
per arm so Arm C alone toggles `--obs-fold`.

---

## 8. Risks

1. **Harbor docs describe a newer release than the stable one.** The `--base-url`/`--api-format`
   flags in `agent-auth-resolution.md` are absent in Harbor **0.24.0** (installed 2026-10-20);
   0.24.0 routes via `OPENAI_BASE_URL` + `MSWEA_API_KEY` env vars (§1.3, §7). Re-verify against the
   pinned Harbor version before the live run, not against the "latest" docs.
2. **mini-swe-agent may stream.** Harbor's wrapper keeps the default `litellm` chat-completions
   path (Responses only when `reasoning_effort` is set — do not pass it), but whether that path
   streams is unverified. B8's ledger refuses `stream=true` (`stream_not_ledgered`); if Harbor's
   mini-swe-agent streams, fall back to a custom `BaseInstalledAgent`/`BaseAgent` calling
   `litellm.completion` non-streaming, or run the ledger on the non-streaming upstream only.
3. **Container → host networking.** `host.docker.internal` is a Docker Desktop (WSL2/macOS) alias;
   a native Linux Docker needs `--add-host host.docker.internal:host-gateway`. Verify on the host.
4. **Polyglot body-stripping is a gap.** Arm B/C degrade non-Python files to raw text (line-window
   skim for C is language-agnostic, but Arm B's "current CF packet" cannot body-strip C/Rust until
   the tree-sitter backend anticipated in `kernel/cartridge/ast_extractor.py` is implemented).
   This is the honest limit of "current CF" on Terminal-Bench; Arm C is the intended winner.
5. **No `repo` dimension** — category-stratification is a new sampling axis; the frozen manifest +
   `assert_frozen_sample` guard must be ported to catch dataset drift (Harbor Hub `latest` can move).
   Pin the dataset `ref`/version, not `latest`, once a commit hash is known.
6. **Harbor Hub auth + dataset download** need `harbor auth login` (an account) or a local
   `--dataset` path; not exercised here (no network/API spend during the B8 window).
7. **Disk** (exploratory, §1.4) is the main local constraint; 50 image pulls may exceed free disk.

---

## 9. References (all opened 2026-10-20)

* `https://www.tbench.ai/news/terminal-bench-2-1` — 2.1 fixes 28 tasks, 89 tasks, 0 unsolved, leaderboard.
* `https://raw.githubusercontent.com/harbor-framework/terminal-bench-2-1/main/README.md` — Harbor is the runner; dataset on Harbor Hub.
* `https://raw.githubusercontent.com/laude-institute/terminal-bench/main/README.md` — `tb` CLI (legacy 1.0/beta) + task/registry overview.
* `https://raw.githubusercontent.com/laude-institute/harbor/main/README.md` — Harbor overview + `harbor run` examples.
* `https://docs.harborframework.com/getting-started/installation.md`, `.../sandboxes/pre-integrated-sandboxes.md` — install + local `docker` sandbox.
* `https://docs.harborframework.com/tasks/overview.md`, `.../configuration.md`, `.../environment.md`, `.../verifier.md` — task format, resources, timeouts, reward file.
* `https://docs.harborframework.com/agents/custom-agents.md`, `.../pre-integrated-agents.md`, `.../agent-auth-resolution.md` — agent plug-in + `--base-url`/`--api-format` routing.
* `https://docs.harborframework.com/jobs/configs.md` — job schema (n_attempts, n_concurrent_trials, timeouts).
* Task manifests (representative) via `https://api.github.com/repos/harbor-framework/terminal-bench-2/contents/<task>/task.toml` — `compile-compcert`, `make-doom-for-mips`, `torch-tensor-parallelism`, `polyglot-c-py`, `configure-git-webserver`, `crack-7z-hash`, `caffe-cifar-10`.
