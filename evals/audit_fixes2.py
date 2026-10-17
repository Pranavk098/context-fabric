"""Audit fixes round 2 — closes the mechanical/partial/outstanding gaps.

  F2   steady-state TTFT (warm-up turn excluded)                [GAP-05]
  F3b  adaptive cartridge + crossover sweep                     [GAP-06/21]
  GAP-17 divergent-context child arm (role slices)              [GAP-17]
  GAP-20 fold-interval sweep                                    [GAP-20]
  GAP-09 cold-boot: hologram vs raw orientation                 [GAP-09]
  GAP-10 negative-knowledge avoidance proxy                     [GAP-10]

Run: uv run python -m evals.audit_fixes2   (needs CF_BASE_URL)
"""

from __future__ import annotations

import json
import os
import statistics
import time
from pathlib import Path
from typing import Any

import httpx

from evals.audit_fixes import (
    DISCOUNT,
    VALUES,
    _ask,
    agg,
    cartridge,
    cpp_fold_state,
    obs_step,
)
from evals.harness import Harness, MetricsScraper, OpenAICompatibleClient, count_tokens
from evals.stage_b2 import facts, pointers

REPORTS = Path(__file__).parent / "reports"
INSTRUCTION = "You are a coding agent. Reply with the single next action."
Messages = list[dict[str, str]]


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _effective(turns: list[Any]) -> tuple[float, float]:
    dyn = sum(t.dynamic_tokens for t in turns)
    cac = sum(t.cached_tokens for t in turns)
    return dyn + DISCOUNT * cac, float(dyn)


def _ttft_ss(turns: list[Any]) -> float:
    tail = turns[1:] if len(turns) > 1 else turns  # F2: drop warm-up turn
    return statistics.fmean(t.ttft_ms for t in tail) if tail else 0.0


def _run_children(
    client: OpenAICompatibleClient, scraper: MetricsScraper, label: str, msgs_list: list[Messages]
) -> dict[str, Any]:
    turns: list[Any] = []
    for m in msgs_list:
        h = Harness(client, label, scraper=scraper)
        h.turn(m, max_tokens=8)
        turns.extend(h.metrics.turns)
    eff, dyn = _effective(turns)
    return {"effective": eff, "dynamic": dyn, "ttft_ss_ms": _ttft_ss(turns)}


# ------------------------------------------------------------------- F2 / F3b crossover
def section_crossover(
    client: OpenAICompatibleClient, scraper: MetricsScraper, reps: int
) -> dict[str, Any]:
    n = 6
    cart = cartridge(40)
    out: dict[str, Any] = {}
    for steps in (3, 5, 8, 12, 20, 30):
        raw = "\n".join(obs_step(i) for i in range(1, steps + 1))
        compiled_full = f"{cart}\n{INSTRUCTION}\n{facts(steps)}\n{pointers(steps)}"
        compiled_adapt = f"{INSTRUCTION}\n{facts(steps)}\n{pointers(steps)}"  # omit cartridge
        acc: dict[str, list[float]] = {"B0_tail": [], "CPP_full": [], "CPP_adaptive": []}
        for rep in range(reps):
            tag = f"s{steps}r{rep}"
            tasks = [f"sub {i}" for i in range(n)]
            arms = {
                "B0_tail": [
                    [
                        {"role": "system", "content": INSTRUCTION},
                        {"role": "user", "content": f"CASE {tag}\n{raw}\nTASK {t}"},
                    ]
                    for t in tasks
                ],
                "CPP_full": [
                    [
                        {"role": "system", "content": f"CASE {tag}\n{compiled_full}"},
                        {"role": "user", "content": f"TASK {t}"},
                    ]
                    for t in tasks
                ],
                "CPP_adaptive": [
                    [
                        {"role": "system", "content": f"CASE {tag}\n{compiled_adapt}"},
                        {"role": "user", "content": f"TASK {t}"},
                    ]
                    for t in tasks
                ],
            }
            for arm, msgs in arms.items():
                acc[arm].append(_run_children(client, scraper, arm, msgs)["effective"])
        out[str(steps)] = {
            arm: {
                "mean": statistics.fmean(v),
                "x_vs_tail": statistics.fmean(acc["B0_tail"]) / statistics.fmean(v)
                if statistics.fmean(v)
                else 0.0,
            }
            for arm, v in acc.items()
        }
    return out


# ------------------------------------------------------------------------- GAP-17 divergent
def section_divergent(
    client: OpenAICompatibleClient, scraper: MetricsScraper, reps: int
) -> dict[str, Any]:
    steps, n = 15, 8
    allobs = [obs_step(i) for i in range(1, steps + 1)]
    cart = cartridge(40)
    shared = f"{cart}\n{INSTRUCTION}\n{facts(steps)}\n{pointers(steps)}"
    res: dict[str, list[float]] = {"B0_role_divergent": [], "CPP_role_shared": []}
    for rep in range(reps):
        tag = f"div{rep}"
        # B0: each child gets a DIFFERENT rotation of the context -> divergent prefixes.
        b0 = [
            [
                {"role": "system", "content": INSTRUCTION},
                {"role": "user", "content": f"CASE {tag}\n" + "\n".join(allobs[i:] + allobs[:i])},
            ]
            for i in range(n)
        ]
        cpp = [
            [
                {"role": "system", "content": f"CASE {tag}\n{shared}"},
                {"role": "user", "content": f"ROLE {i}: audit slice {i}"},
            ]
            for i in range(n)
        ]
        res["B0_role_divergent"].append(
            _run_children(client, scraper, "B0_role_divergent", b0)["effective"]
        )
        res["CPP_role_shared"].append(
            _run_children(client, scraper, "CPP_role_shared", cpp)["effective"]
        )
    cpp_mean = statistics.fmean(res["CPP_role_shared"])
    return {
        arm: {
            "mean": statistics.fmean(v),
            "std": statistics.pstdev(v) if len(v) > 1 else 0.0,
            "x_vs_cpp": statistics.fmean(v) / cpp_mean if cpp_mean else 0.0,
        }
        for arm, v in res.items()
    }


# ------------------------------------------------------------------------- GAP-20 fold sweep
def section_foldsweep(
    client: OpenAICompatibleClient, scraper: MetricsScraper, reps: int
) -> dict[str, Any]:
    steps = 50
    obs = [obs_step(i) for i in range(1, steps + 1)]
    out: dict[str, Any] = {}
    for budget in (600, 1500, 3000, 6000):
        effs: list[float] = []
        rets: list[bool] = []
        for rep in range(reps):
            h = Harness(client, f"fold{budget}", scraper=scraper)
            for s in range(1, steps + 1):
                ledger, live = cpp_fold_state(obs[:s], budget)
                prefix = f"REP{rep}\n{INSTRUCTION}\nLEDGER:\n" + "\n".join(ledger)
                h.turn(
                    [
                        {"role": "system", "content": prefix},
                        {"role": "user", "content": "\n".join(live)},
                    ],
                    max_tokens=8,
                )
            eff, _ = _effective(h.metrics.turns)
            effs.append(eff)
            try:
                ledger, live = cpp_fold_state(obs, budget)
                ans = _ask(
                    client,
                    [
                        {
                            "role": "system",
                            "content": f"REP{rep}\n{INSTRUCTION}\nLEDGER:\n" + "\n".join(ledger),
                        },
                        {"role": "user", "content": "\n".join(live)},
                    ],
                    "What is key1?",
                )
                rets.append(VALUES[1] in ans)
            except httpx.HTTPStatusError:
                rets.append(False)
        out[str(budget)] = {
            "effective": agg(effs),
            "retain_frac": sum(rets) / len(rets),
            "ttft_ss_ms": _ttft_ss(h.metrics.turns),
        }
    return out


# --------------------------------------------------------------------- GAP-09 cold boot
def section_coldboot(client: OpenAICompatibleClient) -> dict[str, Any]:
    holo = "\n".join(f"module_{i} exports symbol_{i}" for i in range(40))
    raw = "\n".join(
        f"# module_{i}.py\n"
        + "\n".join(
            f"  def helper_{i}_{g}(): ...  # references symbol_{(i + g) % 20}" for g in range(6)
        )
        for i in range(20)
    )
    qs = [17, 3, 19]
    holo_ok = raw_ok = 0
    for s in qs:
        a = _ask(
            client,
            [
                {"role": "system", "content": f"Answer from context.\n{holo}"},
                {"role": "user", "content": f"Which module exports symbol_{s}?"},
            ],
            "",
        )
        b = _ask(
            client,
            [
                {"role": "system", "content": f"Answer from context.\n{raw}"},
                {"role": "user", "content": f"Which module exports symbol_{s}?"},
            ],
            "",
        )
        holo_ok += f"module{s}" in _norm(a)
        raw_ok += f"module{s}" in _norm(b)
    return {
        "hologram_tokens": count_tokens(holo),
        "raw_tokens": count_tokens(raw),
        "hologram_accuracy": holo_ok / len(qs),
        "raw_accuracy": raw_ok / len(qs),
    }


# ------------------------------------------------------------------- GAP-10 negative knowledge
SCENARIOS = [
    (
        "fix the failing auth test",
        "raise the session TTL to 24h",
        "session TTL was already raised and did not fix it",
    ),
    (
        "speed up the vector search",
        "add another HNSW layer",
        "adding HNSW layers did not help; it was a connection-pool issue",
    ),
    (
        "reduce token cost",
        "summarize every tool output with an LLM",
        "LLM summarization was tried and cost more than it saved",
    ),
]
BAD = ["ttl", "hnsw", "summar"]


def section_negknowledge(client: OpenAICompatibleClient) -> dict[str, Any]:
    without = with_nk = 0
    total = 0
    for task, bad_action, reason in SCENARIOS:
        total += 1
        base = [
            {
                "role": "system",
                "content": "You are a coding agent. Reply with the single next action.",
            },
            {"role": "user", "content": f"TASK: {task}. What is the next action?"},
        ]
        nk = base[:1] + [
            {
                "role": "user",
                "content": (
                    f"KNOWN DEAD END (do not repeat): {bad_action} — {reason}. "
                    f"TASK: {task}. What is the next action?"
                ),
            }
        ]
        a = _ask(client, base, "")
        b = _ask(client, nk, "")
        idx = SCENARIOS.index((task, bad_action, reason))
        without += BAD[idx] in a.lower()
        with_nk += BAD[idx] in b.lower()
    return {
        "scenarios": total,
        "bad_action_without_nk": without / total,
        "bad_action_with_nk": with_nk / total,
    }


def main() -> int:
    base_url = os.environ.get("CF_BASE_URL", "http://localhost:8000/v1")
    model = os.environ.get("CF_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
    metrics_url = base_url.rsplit("/v1", 1)[0] + "/metrics"
    reps = int(os.environ.get("CF_REPEATS", "2"))
    want = set(os.environ.get("CF_SECTION", "all").split(","))

    def on(n: str) -> bool:
        return "all" in want or n in want

    try:
        up = httpx.get(f"{base_url.rstrip('/')}/models", timeout=3.0).status_code < 500
    except Exception:
        up = False
    if not up:
        print("No endpoint. See docs/STAGE-B-RUNTIME.md (or run scripts/wsl_run_eval.sh).")
        return 0

    client = OpenAICompatibleClient(base_url=base_url, model=model, api_key="EMPTY")
    scraper = MetricsScraper(metrics_url)
    report: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "repeats": reps,
    }

    def guarded(name: str, fn: Any) -> None:
        if not on(name):
            return
        try:
            report[name] = fn()
        except Exception as e:  # keep the run alive; record the failure
            report[f"{name}_error"] = f"{type(e).__name__}: {e}"

    try:
        guarded("crossover", lambda: section_crossover(client, scraper, reps))
        guarded("divergent", lambda: section_divergent(client, scraper, reps))
        guarded("foldsweep", lambda: section_foldsweep(client, scraper, reps))
        guarded("coldboot", lambda: section_coldboot(client))
        guarded("negknowledge", lambda: section_negknowledge(client))
    finally:
        client.close()

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"audit_fixes2_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nReport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
