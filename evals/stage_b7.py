"""Stage B7 — model-tier arbitrage (GAP-08).

Prior attempts saturated (all models ~100% on tiny clean contexts). Arbitrage means: a
small model on a COMPILED packet matches a large model on RAW noise. To force separation we
use a needle-in-haystack retrieval task where the raw context is large and noisy.

Run once per model; the runner sets VLLM_MODEL/CF_MODEL:
  VLLM_MODEL=Qwen/Qwen2.5-0.5B-Instruct  CF_MODEL=same  uv run python -m evals.stage_b7
  VLLM_MODEL=Qwen/Qwen2.5-1.5B-Instruct  CF_MODEL=same  uv run python -m evals.stage_b7
Then:  uv run python -m evals.stage_b7 --aggregate
"""

from __future__ import annotations

import glob
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

from evals.audit_fixes import _ask
from evals.harness import OpenAICompatibleClient, count_tokens

REPORTS = Path(__file__).parent / "reports"
INSTRUCTION = "You are a coding agent. Answer using only the provided context."

N_NEEDLES = 30


def make_needles(seed: int) -> list[tuple[str, str]]:
    rng = random.Random(seed)
    # systematic keys, random values -> retrieval, not pattern-guessing
    return [
        (f"needle_{i} access code", f"code-{rng.randint(100000, 999999)}") for i in range(N_NEEDLES)
    ]


def haystack(seed: int = 5) -> tuple[str, str, list[tuple[str, str]]]:
    rng = random.Random(seed)
    targets = make_needles(seed)
    distractors = [
        f"param_{i} is set to value_{rng.randint(1000, 9999)} for region_{i % 7}."
        for i in range(330)
    ]
    lines = distractors + [f"The {k} is {v}." for k, v in targets]
    rng.shuffle(lines)
    raw = "\n".join(lines)
    compiled = "\n".join(f"The {k} is {v}." for k, v in targets)
    return raw, compiled, targets


def score(ctx: str, items: list[tuple[str, str]]) -> float:
    client = _CLIENT
    assert client is not None
    ok = 0
    for key, val in items:
        a = _ask(
            client,
            [{"role": "system", "content": f"{INSTRUCTION}\n\nCONTEXT:\n{ctx}"}],
            f"What is the {key}?",
        )
        ok += val.lower() in a.lower()
    return ok / len(items)


_CLIENT: OpenAICompatibleClient | None = None


def run() -> dict[str, Any]:
    global _CLIENT
    base_url = os.environ.get("CF_BASE_URL", "http://localhost:8000/v1")
    model = os.environ.get("CF_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
    _CLIENT = OpenAICompatibleClient(base_url=base_url, model=model, api_key="EMPTY")
    raw, compiled, items = haystack()
    try:
        report = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "model": model,
            "raw_tokens": count_tokens(raw),
            "compiled_tokens": count_tokens(compiled),
            "compression": count_tokens(raw) / count_tokens(compiled)
            if count_tokens(compiled)
            else 0.0,
            "raw_accuracy": score(raw, items),
            "compiled_accuracy": score(compiled, items),
        }
    finally:
        _CLIENT.close()
    return report


def aggregate() -> dict[str, Any]:
    files = sorted(glob.glob(str(REPORTS / "stage_b7_*.json")))
    by_model: dict[str, dict[str, Any]] = {}
    for f in files:
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        if "model" in d and "raw_accuracy" in d:
            by_model[d["model"]] = d
    small = next((d for m, d in by_model.items() if "0.5B" in m), None)
    large = next((d for m, d in by_model.items() if "1.5B" in m), None)
    out: dict[str, Any] = {"by_model": by_model}
    if small and large:
        # arbitrage: small-on-compiled vs large-on-raw
        out["arbitrage"] = {
            "small_model": small["model"],
            "large_model": large["model"],
            "small_compiled_acc": small["compiled_accuracy"],
            "large_raw_acc": large["raw_accuracy"],
            "small_raw_acc": small["raw_accuracy"],
            "large_compiled_acc": large["compiled_accuracy"],
            "small_compiled_matches_large_raw": small["compiled_accuracy"]
            >= large["raw_accuracy"] - 0.05,
        }
    return out


def main() -> int:
    if "--aggregate" in sys.argv:
        print(json.dumps(aggregate(), indent=2))
        return 0
    report = run()
    REPORTS.mkdir(parents=True, exist_ok=True)
    tag = report["model"].replace("/", "_")
    out = REPORTS / f"stage_b7_{tag}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
