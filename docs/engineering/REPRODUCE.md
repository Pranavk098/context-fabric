# REPRODUCE — how to re-derive every number we ship

The product's credibility is that its claims can be checked. This file is the shortest path
for someone who is **not** the author to check them. Nothing here needs this machine's setup.

Claims are split into two tiers on purpose. A number is either **recomputed from a committed
artifact** (offline, deterministic, run in CI) or it **needs a live model** (a GPU run you
perform yourself). Nothing in the second tier is ever reported as "reproduced" by us.

## 0. Prerequisites

- Python 3.12+ and [`uv`](https://docs.astral.sh/uv/)
- The repository at any commit; all artifacts live under `evals/reports/` (force-added, so a
  fresh clone has them)
- For the live tier only: a GPU box with vLLM **or** a local Ollama

```bash
uv sync
```

## 1. Offline tier — one command

```bash
uv run cfx repro --verbose
```

This loads `docs/engineering/NUMBERS.md`, and for every `strict` entry:

1. confirms the cited artifact exists,
2. recomputes the value from that artifact's own fields (`a/b`, `a-b`, `a+b`),
3. confirms the displayed string still appears in every doc that quotes it,
4. confirms the entry names its **baseline**.

Expected final line: `cfx repro: OK - <N> offline checks passed`. Any mismatch exits non-zero
and names the entry. This is the same check CI runs (`scripts/check_docs_numbers.py`), so a
drifting number fails the build, not a review.

It does **not** prove model behaviour, tokenizer splits, or timing — those are tier 2.

## 2. Live-engine tier — run these yourself

Expected numbers below are the load-bearing ones; the full per-arm detail is in each doc.

**Repo, no GPU (fixture + compile path):**

```bash
uv run cfx demo --repo . --mock
```

Expected: fixture `7 files / 9,325 raw_tokens`, six objectives, cartridge `cfx_repo_af83f34b`.
Mock accuracy is meaningless and labeled so; only the token accounting is real. (The fixture
walks `*.py` in sorted order to a token target, so the exact file count and cartridge id can
shift if the repo gains files; re-run the command to confirm.)

**Fleet, one engine (GPU):**

```bash
# vLLM
vllm serve Qwen/Qwen2.5-1.5B-Instruct --enable-prefix-caching --max-model-len 8192 --enforce-eager
uv run cfx demo --scenario fleet --upstream http://127.0.0.1:8000

# or Ollama (Windows-native)
ollama pull qwen2.5:1.5b
$env:OLLAMA_CONTEXT_LENGTH="8192"; ollama serve
uv run cfx demo --scenario fleet --upstream http://127.0.0.1:11434 --model qwen2.5:1.5b
```

| arm | prompt tokens | ratio | accuracy |
| :--- | ---: | ---: | ---: |
| broadcast (strongest naive) | 44,854 | — | 6/6 |
| compile | 14,170 | **3.17×** | 6/6 |

Token totals must match exactly; wall time and latency will not (they depend on the box and
cache warmth). See [`ENGINE-MATRIX.md`](ENGINE-MATRIX.md) for the vLLM↔Ollama comparison.

**Real-repo pilot (GPU):**

```bash
git clone --depth 1 https://github.com/encode/httpx && git -C httpx checkout b5addb64f0161ff6bfe94c124ef76f6a1fba5254
# mechanical memory — the headline (13.62× billed):
uv run python -m evals.pilot --fabric mech --fixture <path-to-httpx-clone>
# hand-written blocks — superseded (35.87×):
uv run python -m evals.pilot --fabric hand --fixture <path-to-httpx-clone>
```

Expected: raw prompt tokens `193,366`; compile `14,193` billed (`13.62×`, mechanical) vs
`5,391` (`35.87×`, hand-written blocks — superseded). Accuracy `5/6` vs `5/6` on both. On a
warm prefix cache the raw prompt costs only `182` uncached tokens vs `11,857` for compile, so
the mechanical win is context size and KV memory, not prefill. Design and caveats:
[`PILOT-MECH-RESULT.md`](PILOT-MECH-RESULT.md), [`PILOT.md`](PILOT.md); frozen criteria in
[`PILOT-MECH-PREREGISTRATION.md`](PILOT-MECH-PREREGISTRATION.md) and
[`PILOT-PREREGISTRATION.md`](PILOT-PREREGISTRATION.md).

## 3. Audit trail — seal and replay

Every packet hand-off can be re-derived from its own recorded manifest:

```bash
uv run cfx replay --store fabric.db --packet-id <cpk_...>   # recompile, prove byte-equality or report drift
uv run cfx seal   --store fabric.db --private-key <hex> --out epochs.json
uv run cfx verify epochs.json --pubkey <hex>                # signed epoch chain + Merkle proofs
```

`replay` proves the *notes* an agent received, never the model's answer. See
[`SEAL.md`](SEAL.md).

## 4. Honest limits of reproduction

- **Model-behaviour claims are verified from stored bytes**, not re-sampled, unless you run
  tier 2. `cfx repro` re-scores stored responses where a scorer script exists; it does not
  re-query a model.
- **Tokenizer splits** using the Qwen tokenizer are not re-derivable without `transformers`
  installed. All `o200k` counts recompute; the receipts' `usage.prompt_tokens` match vLLM.
- **KV peaks and throughput** depend on engine version, sampler, and workload; the docs state
  the baseline for each (shared vs unshared raw prefix) and the spread across runs.
- **Pre-registration dating** is filesystem evidence, not cryptographic; a copied file can
  reset mtimes.
- **Baselines matter.** A ratio is reported against its strongest naive arm, named in the
  registry; e.g. the 8.23× throughput is vs an unshared raw broadcast, and is ~2× vs a shared
  raw prefix. Read the `baseline` field before quoting any number.

If a number cannot be reproduced, that is a bug in the docs — open an issue with the exact
command and the observed value.
