"""Stage B3 — compression/scale sweep for the multi-agent fork win.

Stage B2 (docs/STAGE-B-FINDINGS.md §7) showed CPP_fork beats naive broadcast because the
compiled shared prefix is smaller than the raw transcript. B3 quantifies that lever:

  * sweep parent context size (how much raw transcript the children must share),
  * sweep child count N,
and report effective-cost ratios CPP vs B0_tail / B0_top.

Run: uv run python -m evals.stage_b3   (needs CF_BASE_URL; see docs/engineering/RUNTIME.md)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from evals.harness import MetricsScraper, OpenAICompatibleClient
from evals.stage_b2 import (
    CARTRIDGE,
    INSTRUCTION,
    b0_child_tail,
    b0_child_top,
    cpp_child,
    facts,
    pointers,
    run_children,
)

REPORTS = Path(__file__).parent / "reports"


def obs_block(i: int, lines: int) -> str:
    return f"=== tool result {i} ===\n" + "\n".join(
        f"  line {j}: raw source or log content {'x' * 12}" for j in range(lines)
    )


def raw_shared(steps: int, lines: int = 10) -> str:
    return "\n".join(obs_block(i, lines) for i in range(steps))


def compiled_shared(steps: int) -> str:
    return f"{CARTRIDGE}\n{INSTRUCTION}\nLEDGER:\n{facts(steps)}\nSWAP:\n{pointers(steps)}"


def tasks(n: int) -> list[str]:
    return [
        f"sub-task {i}: {'audit' if i % 2 else 'implement'} the retrieval path" for i in range(n)
    ]


def fork_suite(
    client: OpenAICompatibleClient, scraper: MetricsScraper, steps: int, n: int, tag: str
) -> dict[str, Any]:
    # A unique leading tag makes each case's shared prefix distinct, so vLLM's persistent
    # prefix cache cannot carry over (there is no reset endpoint in this build).
    raw = f"CASE {tag}\n" + raw_shared(steps)
    compiled = f"CASE {tag}\n" + compiled_shared(steps)
    tks = tasks(n)
    return {
        "parent_steps": steps,
        "children": n,
        "B0_tail": run_children(client, scraper, "B0_tail", [b0_child_tail(raw, t) for t in tks]),
        "B0_top": run_children(client, scraper, "B0_top", [b0_child_top(raw, t) for t in tks]),
        "CPP_fork": run_children(
            client, scraper, "CPP_fork", [cpp_child(compiled, "", t) for t in tks]
        ),
    }


def ratio(case: dict[str, Any]) -> dict[str, float]:
    cpp = case["CPP_fork"]["effective_tokens"]
    return {
        "B0_tail_over_CPP": case["B0_tail"]["effective_tokens"] / cpp if cpp else 0.0,
        "B0_top_over_CPP": case["B0_top"]["effective_tokens"] / cpp if cpp else 0.0,
    }


def main() -> int:
    base_url = os.environ.get("CF_BASE_URL", "http://localhost:8000/v1")
    model = os.environ.get("CF_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
    base_root = base_url.rsplit("/v1", 1)[0]
    try:
        up = httpx.get(f"{base_url.rstrip('/')}/models", timeout=3.0).status_code < 500
    except Exception:
        up = False
    if not up:
        print("No endpoint. See docs/engineering/RUNTIME.md (or run scripts/wsl_run_eval.sh).")
        return 0

    client = OpenAICompatibleClient(
        base_url=base_url, model=model, api_key=os.environ.get("CF_API_KEY", "EMPTY")
    )
    scraper = MetricsScraper(base_root + "/metrics")
    report: dict[str, Any] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": model, "cases": []}
    try:
        for steps in (5, 15, 30):
            case = fork_suite(client, scraper, steps, n=8, tag=f"s{steps}n8")
            case["ratios"] = ratio(case)
            report["cases"].append(case)
        for n in (4, 16):
            case = fork_suite(client, scraper, 15, n=n, tag=f"s15n{n}")
            case["ratios"] = ratio(case)
            report["cases"].append(case)
    finally:
        client.close()

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"stage_b3_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(
        f"{'steps':>5} {'kids':>4} | {'B0_tail eff':>11} {'B0_top eff':>11} {'CPP eff':>9} "
        f"| {'×vs tail':>8} {'×vs top':>8}"
    )
    for c in report["cases"]:
        r = c["ratios"]
        print(
            f"{c['parent_steps']:>5} {c['children']:>4} | "
            f"{c['B0_tail']['effective_tokens']:>11.0f} {c['B0_top']['effective_tokens']:>11.0f} "
            f"{c['CPP_fork']['effective_tokens']:>9.0f} | {r['B0_tail_over_CPP']:>7.2f}× "
            f"{r['B0_top_over_CPP']:>7.2f}×"
        )
    print(f"\nReport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
