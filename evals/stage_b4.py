"""Stage B4 — quality parity: does compiled context preserve task success?

Pre-kernel proxy for the compiler. A labeled extractive-QA set over a noisy context:

  * raw_full   : every fact embedded among distractor log lines (the naive broadcast).
  * cpp_ideal  : only the structured facts (lossless extraction; noise removed).
  * cpp_lossy  : facts with a fraction dropped (simulated lossy compression).

Sweeps the lossy fraction -> the quality-vs-compression curve. This tests GAP-11 (quality
regression) and GAP-04 (compression reality) in docs/SHORTFALL-AUDIT.md.

Run: uv run python -m evals.stage_b4
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

from evals.harness import OpenAICompatibleClient, count_tokens

REPORTS = Path(__file__).parent / "reports"
SYSTEM = (
    "Answer the question using ONLY the provided context. Reply with just the answer, nothing else."
)

FACTS: list[tuple[str, str]] = [
    ("api base path", "/v2/search"),
    ("request timeout", "4500"),
    ("retry limit", "3"),
    ("cache ttl", "900"),
    ("auth header", "x-api-key"),
    ("database port", "5433"),
    ("vector dimension", "1536"),
    ("batch size", "64"),
    ("max connections", "128"),
    ("upload size limit", "25"),
    ("session length", "3600"),
    ("shard count", "12"),
]


def fact_line(key: str, val: str) -> str:
    return f"The {key} is {val}."


def distractors(n: int = 24) -> list[str]:
    return [
        f"log line {i}: processed request id={i:04d} latency={20 + i}ms status=200 ok"
        for i in range(n)
    ]


def raw_context() -> str:
    facts = [fact_line(k, v) for k, v in FACTS]
    dist = distractors()
    interleaved: list[str] = []
    for i, f in enumerate(facts):
        interleaved.append(f)
        interleaved.append(dist[i % len(dist)])
    interleaved.extend(dist[len(facts) :])
    return "\n".join(interleaved)


def compiled_context(drop_fraction: float, seed: int = 7) -> tuple[str, set[str]]:
    """Return (context, dropped_keys). Deterministic drop order."""
    order = sorted(range(len(FACTS)), key=lambda i: (i * 2654435761 + seed) % 9973)
    n_drop = int(round(drop_fraction * len(FACTS)))
    dropped_idx = set(order[:n_drop])
    kept = [fact_line(k, v) for i, (k, v) in enumerate(FACTS) if i not in dropped_idx]
    dropped_keys = {FACTS[i][0] for i in dropped_idx}
    return "\n".join(kept), dropped_keys


def ask(client: OpenAICompatibleClient, context: str, question: str) -> str:
    messages = [
        {"role": "system", "content": f"{SYSTEM}\n\nCONTEXT:\n{context}"},
        {"role": "user", "content": question},
    ]
    payload = {
        "model": client.model,
        "messages": messages,
        "max_tokens": 16,
        "temperature": 0.0,
    }
    r = httpx.post(
        f"{client.base_url}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {client.api_key}"},
        timeout=120.0,
    )
    r.raise_for_status()
    return str(r.json()["choices"][0]["message"]["content"])


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def score_arm(label: str, context: str, dropped_keys: set[str]) -> dict[str, Any]:
    correct_answers: list[dict[str, Any]] = []
    client = _CLIENT
    assert client is not None
    for key, val in FACTS:
        q = f"What is the {key}?"
        out = ask(client, context, q)
        correct = norm(val) in norm(out)
        # A question whose fact was dropped is *expected* to fail; track it explicitly.
        dropped = key in dropped_keys
        correct_answers.append(
            {
                "key": key,
                "gold": val,
                "out": out.strip(),
                "correct": correct,
                "fact_dropped": dropped,
            }
        )
    answerable = [a for a in correct_answers if not a["fact_dropped"]]
    return {
        "label": label,
        "context_tokens": count_tokens(context),
        "accuracy": sum(a["correct"] for a in correct_answers) / len(correct_answers),
        "accuracy_on_retained": (
            sum(a["correct"] for a in answerable) / len(answerable) if answerable else 0.0
        ),
        "detail": correct_answers,
    }


_CLIENT: OpenAICompatibleClient | None = None


def main() -> int:
    global _CLIENT
    base_url = os.environ.get("CF_BASE_URL", "http://localhost:8000/v1")
    model = os.environ.get("CF_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
    try:
        up = httpx.get(f"{base_url.rstrip('/')}/models", timeout=3.0).status_code < 500
    except Exception:
        up = False
    if not up:
        print("No endpoint. See docs/engineering/RUNTIME.md (or run scripts/wsl_run_eval.sh).")
        return 0

    _CLIENT = OpenAICompatibleClient(
        base_url=base_url, model=model, api_key=os.environ.get("CF_API_KEY", "EMPTY")
    )
    report: dict[str, Any] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": model, "arms": []}
    try:
        raw = raw_context()
        report["arms"].append(score_arm("raw_full", raw, set()))
        for p in (0.0, 0.25, 0.5, 0.75):
            ctx, dropped = compiled_context(p)
            report["arms"].append(score_arm(f"cpp_lossy_{int(p * 100)}", ctx, dropped))
    finally:
        _CLIENT.close()

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"stage_b4_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"{'arm':>16} | {'ctx tok':>7} | {'acc':>6} | {'acc|retained':>12}")
    for a in report["arms"]:
        print(
            f"{a['label']:>16} | {a['context_tokens']:>7} | {a['accuracy']:>6.2f} | "
            f"{a['accuracy_on_retained']:>12.2f}"
        )
    print(f"\nReport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
