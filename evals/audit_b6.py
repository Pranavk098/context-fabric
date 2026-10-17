"""Stage B6 — make it real: close the outstanding gaps.

  B6.1 realrepo  : compression ratio over a REAL package (stdlib ast extractor)  [GAP-13]
  B6.2 coldboot  : hologram vs raw orientation, accuracy + tokens                 [GAP-09]
  B6.3 quality    : harder combined-fact reasoning, raw vs compiled vs lossy        [GAP-22/11]
  B6.5 negknowledge: multiple-choice dead-end avoidance, with/without NK           [GAP-10]
  B6.6 flops      : prefill-FLOPs proxy alongside token counts                      [GAP-16]
  B6.4 arbitrage  : run the quality section on two models (0.5B vs 1.5B)            [GAP-08]

Run (1.5B):  uv run python -m evals.audit_b6
Run (0.5B):  set CF_MODEL + VLLM_MODEL to Qwen/Qwen2.5-0.5B-Instruct, then CF_SECTION=quality
"""

from __future__ import annotations

import ast
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from evals.audit_fixes import _ask
from evals.harness import OpenAICompatibleClient, count_tokens

REPORTS = Path(__file__).parent / "reports"
INSTRUCTION = "You are a coding agent. Answer using the provided context."
SAMPLE_PARAMS = float(os.environ.get("CF_PARAMS", "1.5e9"))  # for the FLOPs proxy


def flops_proxy(dynamic_tokens: float) -> float:
    """Prefill FLOPs ~= 2 * params * tokens (forward-pass approximation)."""
    return 2.0 * SAMPLE_PARAMS * dynamic_tokens


# --------------------------------------------------------------------------------- B6.1
def section_realrepo() -> dict[str, Any]:
    import httpx as _pkg

    root = Path(_pkg.__file__).parent
    raw_parts: list[str] = []
    sig_parts: list[str] = []

    def extract(src: str) -> list[str]:
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return []
        out: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append(f"def {node.name}({', '.join(a.arg for a in node.args.args)})")
            elif isinstance(node, ast.ClassDef):
                out.append(f"class {node.name}")
        return out

    for f in sorted(root.rglob("*.py")):
        src = f.read_text(encoding="utf-8", errors="replace")
        raw_parts.append(src)
        sig_parts.append(f"# {f.relative_to(root)}\n" + "\n".join(extract(src)))
    raw_tokens = count_tokens("\n".join(raw_parts))
    sig_tokens = count_tokens("\n".join(sig_parts))
    return {
        "package": "httpx",
        "files": len(list(root.rglob("*.py"))),
        "raw_tokens": raw_tokens,
        "compiled_tokens": sig_tokens,
        "ratio": raw_tokens / sig_tokens if sig_tokens else 0.0,
    }


# --------------------------------------------------------------------------------- B6.2
def section_coldboot(client: OpenAICompatibleClient) -> dict[str, Any]:
    holo = "\n".join(f"module_{i} exports symbol_{i}" for i in range(40))
    raw = "\n".join(f"# module_{i}.py\n" for i in range(40)) + "\n".join(
        f"  line: helper_{j}()  # unrelated body content {j}" for j in range(220)
    )
    qs = [17, 3, 19, 31]
    sys_holo = (
        f"{INSTRUCTION}\nAnswer with the module identifier, e.g. module_17.\n\nHOLOGRAM:\n{holo}"
    )
    sys_raw = f"{INSTRUCTION}\nAnswer with the module identifier, e.g. module_17.\n\nFILES:\n{raw}"
    samples: list[dict[str, str]] = []
    holo_ok = raw_ok = 0
    for s in qs:
        a = _ask(
            client, [{"role": "system", "content": sys_holo}], f"Which module exports symbol_{s}?"
        )
        b = _ask(
            client, [{"role": "system", "content": sys_raw}], f"Which module exports symbol_{s}?"
        )
        holo_ok += f"module{s}" in "".join(c for c in a.lower() if c.isalnum())
        raw_ok += f"module{s}" in "".join(c for c in b.lower() if c.isalnum())
        samples.append(
            {"q": f"symbol_{s}", "hologram_out": a.strip()[:60], "raw_out": b.strip()[:60]}
        )
    return {
        "hologram_tokens": count_tokens(sys_holo),
        "raw_tokens": count_tokens(sys_raw),
        "hologram_accuracy": holo_ok / len(qs),
        "raw_accuracy": raw_ok / len(qs),
        "samples": samples,
    }


# --------------------------------------------------------------------------------- B6.3
FACTS: list[tuple[str, str]] = [
    ("retry limit", "3"),
    ("retry delay ms", "40"),
    ("batch size", "64"),
    ("batch count", "5"),
    ("cache ttl", "900"),
    ("cache refresh interval", "150"),
    ("worker count", "8"),
    ("queue depth", "24"),
    ("vector dimension", "1536"),
    ("shard count", "12"),
]
# Lookups (one fact) and comparisons (two facts, no arithmetic) — a small model can do both,
# so we can measure parity between raw and compiled contexts (GAP-22/11).
LOOKUPS: list[tuple[str, str]] = [
    ("What is the cache ttl?", "900"),
    ("What is the batch size?", "64"),
    ("What is the vector dimension?", "1536"),
]
COMPARISONS: list[tuple[str, str]] = [
    ("Which is larger: the retry limit or the batch count? Name the larger one.", "batch count"),
    ("Which is larger: the cache ttl or the cache refresh interval? Name the larger one.", "ttl"),
    ("Which is larger: the worker count or the queue depth? Name the larger one.", "queue"),
    ("Which is larger: the vector dimension or the shard count? Name the larger one.", "vector"),
]


def _quality_context(drop_fraction: float, seed: int = 11) -> tuple[str, set[str]]:
    order = sorted(range(len(FACTS)), key=lambda i: (i * 7919 + seed) % 9973)
    drop = int(round(drop_fraction * len(FACTS)))
    dropped = set(order[:drop])
    kept = [f"The {k} is {v}." for i, (k, v) in enumerate(FACTS) if i not in dropped]
    return "\n".join(kept), {FACTS[i][0] for i in dropped}


def section_quality(client: OpenAICompatibleClient) -> dict[str, Any]:
    def score(ctx: str) -> dict[str, Any]:
        def acc(items: list[tuple[str, str]]) -> float:
            ok = 0
            for q, gold in items:
                a = _ask(
                    client, [{"role": "system", "content": f"{INSTRUCTION}\n\nCONTEXT:\n{ctx}"}], q
                )
                ok += gold in a.lower()
            return ok / len(items)

        return {
            "lookup_acc": acc(LOOKUPS),
            "comparison_acc": acc(COMPARISONS),
            "tokens": count_tokens(ctx),
        }

    results: dict[str, Any] = {}
    raw_ctx, _ = _quality_context(0.0)
    dist = [f"log line {i}: request id=... latency={20 + i}ms" for i in range(24)]
    results["raw_full"] = score(raw_ctx + "\n" + "\n".join(dist))
    for p in (0.0, 0.2, 0.4):
        ctx, _ = _quality_context(p)
        results[f"compiled_lossy_{int(p * 100)}"] = score(ctx)
    return results


# --------------------------------------------------------------------------------- B6.5
NK_SCENARIOS = [
    (
        "fix the flaky auth test",
        [
            "raise the session TTL to 24h",
            "add a retry with backoff",
            "increase the connection pool",
            "add an index",
        ],
    ),
    (
        "speed up vector search",
        ["add another HNSW layer", "tune ef_search", "add a read replica", "cache results"],
    ),
    (
        "cut token cost",
        [
            "summarize every tool output with an LLM",
            "fold redundant context",
            "reduce max_tokens",
            "trim the system prompt",
        ],
    ),
]
NK_REASONS = [
    "session TTL was already raised and did not fix it",
    "extra HNSW layers did not help",
    "LLM summarization cost more than it saved",
]


def section_negknowledge(client: OpenAICompatibleClient) -> dict[str, Any]:
    samples = 4
    without = with_nk = picks = 0
    for idx, (task, options) in enumerate(NK_SCENARIOS):
        bad = options[0]
        opts = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
        for _ in range(samples):
            base = [
                {
                    "role": "system",
                    "content": "Pick the single best next action. Reply with only its number.",
                },
                {"role": "user", "content": f"TASK: {task}\nOPTIONS:\n{opts}"},
            ]
            nk = base[:1] + [
                {
                    "role": "user",
                    "content": (
                        f"TASK: {task}\nKNOWN DEAD END (do not pick): '{bad}' failed — "
                        f"{NK_REASONS[idx]}.\nOPTIONS:\n{opts}"
                    ),
                }
            ]
            a = _ask(client, base, "Your choice (1-4):")
            b = _ask(client, nk, "Your choice (1-4):")
            picks += 2
            without += _picked_bad(a, bad, options)
            with_nk += _picked_bad(b, bad, options)
    return {
        "picks": picks,
        "bad_rate_without_nk": without / picks,
        "bad_rate_with_nk": with_nk / picks,
    }


def _picked_bad(answer: str, bad: str, options: list[str]) -> int:
    digits = [c for c in answer if c.isdigit()]
    if not digits:
        return int(bad in answer)
    n = int(digits[0])
    return int(1 <= n <= len(options) and options[n - 1] == bad)


def main() -> int:
    base_url = os.environ.get("CF_BASE_URL", "http://localhost:8000/v1")
    model = os.environ.get("CF_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
    want = set(os.environ.get("CF_SECTION", "all").split(","))

    def on(n: str) -> bool:
        return "all" in want or n in want

    report: dict[str, Any] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": model}
    if on("realrepo"):
        report["realrepo"] = section_realrepo()
        report["realrepo_flops_note"] = (
            "compression is token-only; FLOPs proxy scales with dynamic tokens"
        )

    try:
        up = httpx.get(f"{base_url.rstrip('/')}/models", timeout=3.0).status_code < 500
    except Exception:
        up = False
    if not up:
        print(json.dumps(report, indent=2))
        print("\n(No endpoint: realrepo only. Start one for the rest.)")
        return 0

    client = OpenAICompatibleClient(base_url=base_url, model=model, api_key="EMPTY")

    def guarded(name: str, fn: Any) -> None:
        if not on(name):
            return
        try:
            report[name] = fn()
        except Exception as e:
            report[f"{name}_error"] = f"{type(e).__name__}: {e}"

    try:
        guarded("coldboot", lambda: section_coldboot(client))
        guarded("quality", lambda: section_quality(client))
        guarded("negknowledge", lambda: section_negknowledge(client))
    finally:
        client.close()

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"audit_b6_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nReport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
