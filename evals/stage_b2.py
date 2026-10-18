"""Stage B2 — corrected intra-agent + decisive multi-agent fork experiment.

Builds on the Stage B finding (docs/STAGE-B-FINDINGS.md): within one agent, prefix
caching already makes a naive append-only broadcast a strong baseline. The packet
approach's real value must therefore show where prefix caching CANNOT reach:

  * several agents sharing one project prefix,
  * role-differentiated context (agents that see different slices).

Arms
----
Intra-agent (fixes the F3 render-order defect: growth only at the tail, Tier 1/2 frozen
inside an epoch):
  * B0_intra     : append-only raw observations, constant system.
  * CPP_intra    : cartridge + ledger/pointers folded every k steps, live obs appended.

Multi-agent fork (parent builds a shared ledger, then N children):
  * B0_tail      : naive, task at the TAIL  -> shared prefix preserved (best case for B0).
  * B0_top       : naive, task at the TOP    -> prefix broken per child (realistic naive).
  * CPP_fork     : children share Tier 0 + Tier 1 prefix, role/task live in the Tier 3 tail.

Decision metric: effective billed tokens = uncached + DISCOUNT * cached, plus cache-hit
ratio and latency. DISCOUNT defaults to 0.1 (90% prompt-cache discount).

Run: uv run python -m evals.stage_b2   (needs CF_BASE_URL; see docs/engineering/RUNTIME.md)
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from evals.harness import Harness, MetricsScraper, OpenAICompatibleClient

STEPS = 15
EPOCH = 3
N_CHILDREN = 8
DISCOUNT = float(os.environ.get("CF_CACHE_DISCOUNT", "0.1"))
REPORTS = Path(__file__).parent / "reports"

Messages = list[dict[str, str]]
Builder = Callable[[int, list[str]], Messages]

CARTRIDGE = "## PROJECT HOLOGRAM (Tier 0)\n" + "\n".join(
    f"- module_{i}: exports symbol_{i}() used by module_{i + 1}" for i in range(40)
)
INSTRUCTION = "You are a coding agent. Reply with the single next action."


def synth_obs(n: int) -> list[str]:
    return [
        f"=== tool result {i} ===\n"
        + "\n".join(f"  line {j}: raw source or log content {'x' * 12}" for j in range(20))
        for i in range(n)
    ]


def facts(n: int) -> str:
    return "\n".join(f"FACT {i + 1}: step {i + 1} done (1-line summary)" for i in range(n))


def pointers(n: int) -> str:
    return "\n".join(f"ctx://obs/{i + 1} <raw evicted, 20 lines>" for i in range(n))


# ---------------------------------------------------------------- intra-agent builders
def b0_intra_builder() -> Builder:
    def build(step: int, history: list[str]) -> Messages:
        return [
            {"role": "system", "content": INSTRUCTION},
            {"role": "user", "content": "\n".join(history)},
        ]

    return build


def cpp_intra_builder() -> Builder:
    def build(step: int, history: list[str]) -> Messages:
        folded = ((step - 1) // EPOCH) * EPOCH  # steps fully folded into Tier 1/2
        prefix = f"{CARTRIDGE}\n{INSTRUCTION}\nLEDGER:\n{facts(folded)}\nSWAP:\n{pointers(folded)}"
        live = "\n".join(history[folded:])  # append-only within the epoch
        return [{"role": "system", "content": prefix}, {"role": "user", "content": live}]

    return build


# ------------------------------------------------------------------ multi-agent builders
def b0_child_tail(shared_raw: str, task: str) -> Messages:
    return [
        {"role": "system", "content": INSTRUCTION},
        {"role": "user", "content": f"{shared_raw}\nROLE/TASK: {task}"},
    ]


def b0_child_top(shared_raw: str, task: str) -> Messages:
    return [
        {"role": "system", "content": INSTRUCTION},
        {"role": "user", "content": f"ROLE/TASK: {task}\n{shared_raw}"},
    ]


def cpp_child(shared_prefix: str, live: str, task: str) -> Messages:
    return [
        {"role": "system", "content": shared_prefix},
        {"role": "user", "content": f"ROLE/TASK: {task}\n{live}"},
    ]


# ------------------------------------------------------------------------------- runners
def _summarize(label: str, model: str, turns: list[Any]) -> dict[str, Any]:
    total_prompt = sum(t.prompt_tokens for t in turns)
    total_cached = sum(t.cached_tokens for t in turns)
    total_dynamic = sum(t.dynamic_tokens for t in turns)
    effective = total_dynamic + DISCOUNT * total_cached
    return {
        "label": label,
        "prompt_tokens_total": total_prompt,
        "cached_tokens_total": total_cached,
        "dynamic_tokens_total": total_dynamic,
        "effective_tokens": effective,
        "cache_hit_ratio": (total_cached / total_prompt) if total_prompt else 0.0,
        "mean_ttft_ms": (sum(t.ttft_ms for t in turns) / len(turns)) if turns else 0.0,
    }


def run_sequence(
    client: OpenAICompatibleClient,
    scraper: MetricsScraper,
    label: str,
    builder: Builder,
    outputs: list[str],
) -> dict[str, Any]:
    harness = Harness(client, label, scraper=scraper)
    history: list[str] = []
    for step in range(1, STEPS + 1):
        history.append(outputs[step - 1])
        harness.turn(builder(step, history), max_tokens=16)
    return _summarize(label, client.model, harness.metrics.turns)


def run_children(
    client: OpenAICompatibleClient,
    scraper: MetricsScraper,
    label: str,
    child_msgs: list[Messages],
) -> dict[str, Any]:
    turns: list[Any] = []
    for msgs in child_msgs:
        harness = Harness(client, label, scraper=scraper)
        harness.turn(msgs, max_tokens=16)
        turns.extend(harness.metrics.turns)
    return _summarize(label, client.model, turns)


# ------------------------------------------------------------------------------------ main
def _up(base_url: str) -> bool:
    import httpx

    try:
        with httpx.Client(timeout=3.0) as c:
            return c.get(f"{base_url.rstrip('/')}/models").status_code < 500
    except Exception:
        return False


def main() -> int:
    base_url = os.environ.get("CF_BASE_URL", "http://localhost:8000/v1")
    model = os.environ.get("CF_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
    if not _up(base_url):
        print("No endpoint. See docs/engineering/RUNTIME.md (or run scripts/wsl_run_eval.sh).")
        return 0

    client = OpenAICompatibleClient(
        base_url=base_url, model=model, api_key=os.environ.get("CF_API_KEY", "EMPTY")
    )
    scraper = MetricsScraper(base_url.rsplit("/v1", 1)[0] + "/metrics")
    outputs = synth_obs(STEPS)
    report: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "discount": DISCOUNT,
    }

    try:
        # --- intra-agent (corrected) ---
        report["intra"] = {
            "B0": run_sequence(client, scraper, "B0_intra", b0_intra_builder(), outputs),
            "CPP": run_sequence(client, scraper, "CPP_intra", cpp_intra_builder(), outputs),
        }

        # --- multi-agent fork ---
        shared_raw = "\n".join(outputs)  # B0 broadcast: the entire raw history, once
        folded = STEPS  # parent settles all steps into the ledger at watermark W
        shared_prefix = (
            f"{CARTRIDGE}\n{INSTRUCTION}\nLEDGER:\n{facts(folded)}\nSWAP:\n{pointers(folded)}"
        )
        tasks = [
            f"sub-task {i}: {'audit' if i % 2 else 'implement'} the retrieval path"
            for i in range(N_CHILDREN)
        ]
        live = ""  # children start with an empty working register (CoW fork)

        report["fork"] = {
            "B0_tail": run_children(
                client, scraper, "B0_tail", [b0_child_tail(shared_raw, t) for t in tasks]
            ),
            "B0_top": run_children(
                client, scraper, "B0_top", [b0_child_top(shared_raw, t) for t in tasks]
            ),
            "CPP_fork": run_children(
                client, scraper, "CPP_fork", [cpp_child(shared_prefix, live, t) for t in tasks]
            ),
        }
    finally:
        client.close()

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"stage_b2_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps({"intra": report["intra"], "fork": report["fork"]}, indent=2))
    print(f"\nReport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
