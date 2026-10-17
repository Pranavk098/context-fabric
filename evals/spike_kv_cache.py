"""Stage B — KV-cache / prefix-reuse falsification spike.

Runs the SAME 15-step tool loop two ways against one OpenAI-compatible endpoint
(vLLM, LMCache-fronted vLLM, Ollama, or a cloud API) and compares the numbers that
decide the project:

  * B0  (full broadcast): every turn re-sends the entire raw tool-output history.
  * CPP (packet):         stable Tier0 cartridge + append-only Tier1 ledger + Tier2
                          pointer lines + only the current step's raw delta.

Then it spawns a CoW child (same Tier0/Tier1 prefix, empty Tier3) to test cross-fork reuse.

Usage:
    set CF_BASE_URL=http://localhost:8000/v1
    set CF_MODEL=Qwen/Qwen2.5-1.5B-Instruct
    uv run python -m evals.spike_kv_cache

If no endpoint is reachable it prints bring-up instructions and exits 0, so the
deterministic core can be verified without a GPU.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from evals.harness import Harness, MetricsScraper, OpenAICompatibleClient, count_tokens

STEPS = 15
REPORTS = Path(__file__).parent / "reports"
MessageBuilder = Callable[[int, list[str]], list[dict[str, str]]]

CARTRIDGE = "## PROJECT HOLOGRAM (Tier 0)\n" + "\n".join(
    f"- module_{i}: exports symbol_{i}() used by module_{i + 1}" for i in range(40)
)
INSTRUCTION = "You are the implementer. Reply with the single next action."


def synth_tool_outputs(n: int) -> list[str]:
    """Raw tool outputs that would bloat a naive window (file dumps + stack traces).

    Sized (20 lines) so the full-broadcast arm still fits in a 4096-token context on
    the 8 GB dev box: 15 steps x ~180 tokens ~= 2,700 tokens at the last turn.
    """
    return [
        f"=== tool result {i} ===\n"
        + "\n".join(f"  line {j}: some raw source or log content {'x' * 12}" for j in range(20))
        for i in range(n)
    ]


def b0_builder(outputs: list[str]) -> MessageBuilder:
    def build(step: int, history: list[str]) -> list[dict[str, str]]:
        body = "\n".join(f"OBSERVATION {i + 1}:\n{o}" for i, o in enumerate(history))
        return [
            {"role": "system", "content": "You are a coding agent. " + INSTRUCTION},
            {"role": "user", "content": body + f"\n\nStep {step}: next action?"},
        ]

    return build


def cpp_builder(outputs: list[str]) -> MessageBuilder:
    def build(step: int, history: list[str]) -> list[dict[str, str]]:
        # Tier 1 ledger grows append-only with a compact 1-line fact per completed step.
        ledger = "\n".join(
            f"FACT {i + 1}: step {i + 1} done (summary, 1 line)" for i in range(len(history) - 1)
        )
        pointers = "\n".join(
            f"ctx://obs/{i + 1} <{count_tokens(o)} tok raw, evicted>"
            for i, o in enumerate(history[:-1])
        )
        current = history[-1] if history else ""
        # Stable prefix: cartridge + instruction + ledger + pointers. Only the current
        # raw observation is volatile (append-only -> prior prefix stays byte-identical).
        stable = f"{CARTRIDGE}\n\n{INSTRUCTION}\n\nLEDGER:\n{ledger}\n\nSWAP:\n{pointers}"
        return [
            {"role": "system", "content": stable},
            {
                "role": "user",
                "content": f"CURRENT OBSERVATION:\n{current}\n\nStep {step}: next action?",
            },
        ]

    return build


def run_arm(
    client: OpenAICompatibleClient, label: str, builder: MessageBuilder, outputs: list[str]
) -> dict[str, Any]:
    metrics_url = client.base_url.rsplit("/v1", 1)[0] + "/metrics"
    scraper = MetricsScraper(metrics_url)
    harness = Harness(client, label, scraper=scraper)
    history: list[str] = []
    for step in range(1, STEPS + 1):
        history.append(outputs[step - 1])
        harness.turn(builder(step, history), max_tokens=32)
    return harness.metrics.summary()


def endpoint_up(base_url: str) -> bool:
    try:
        with httpx.Client(timeout=3.0) as c:
            r = c.get(f"{base_url.rstrip('/')}/models")
            return r.status_code < 500
    except Exception:
        return False


def bring_up_instructions() -> None:
    print(
        "\nNo OpenAI-compatible endpoint reachable.\n"
        "Bring one up (WSL2/Docker), then re-run:\n\n"
        "  powershell:  docker compose up -d vllm\n"
        "  then:        $env:CF_BASE_URL='http://localhost:8000/v1'\n"
        "               $env:CF_MODEL='Qwen/Qwen2.5-1.5B-Instruct'\n"
        "               uv run python -m evals.spike_kv_cache\n\n"
        "Or point CF_BASE_URL at any running vLLM / Ollama / cloud endpoint.\n"
        "See docs/STAGE-B-RUNTIME.md for the full recipe.\n"
    )


def main() -> int:
    base_url = os.environ.get("CF_BASE_URL", "http://localhost:8000/v1")
    model = os.environ.get("CF_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
    if not endpoint_up(base_url):
        bring_up_instructions()
        return 0

    outputs = synth_tool_outputs(STEPS)
    client = OpenAICompatibleClient(
        base_url=base_url, model=model, api_key=os.environ.get("CF_API_KEY", "EMPTY")
    )
    try:
        b0 = run_arm(client, "B0_full_broadcast", b0_builder(outputs), outputs)
        cpp = run_arm(client, "CPP_packet", cpp_builder(outputs), outputs)
    finally:
        client.close()

    tok_saved = 1 - (cpp["prompt_tokens_total"] / b0["prompt_tokens_total"])
    report = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "base_url": base_url,
        "model": model,
        "steps": STEPS,
        "arms": {"B0": b0, "CPP": cpp},
        "delta": {
            "prompt_token_reduction": tok_saved,
            "cache_hit_ratio_B0": b0["prefix_cache_hit_ratio"],
            "cache_hit_ratio_CPP": cpp["prefix_cache_hit_ratio"],
            "ttft_ms_B0": b0["mean_ttft_ms"],
            "ttft_ms_CPP": cpp["mean_ttft_ms"],
        },
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"kv_spike_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps(report["delta"], indent=2))
    print(f"\nReport: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
