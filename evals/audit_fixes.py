"""Post-B5 audit fixes — implements the F1…F8 fixes from docs/POST-B5-AUDIT.md.

Default is to FIX, not rescope. Sections:

  F8 15x claim     : measure single-chat vs multi-agent token ratio (GAP-01).
  F7 compression   : realistic deterministic extractor ratio over synthetic code (GAP-04/25).
  F4 intra-long    : bounded-context retention — B0 unbounded vs B0 window vs CPP fold (GAP-02/20).
  F5 fork fair     : B0_top / B0_tail / CPP_v2, REPEATS with mean+-std (GAP-12/17/27).
  F6 concurrency   : B0_divergent / B0_tail(shared) / CPP_shared, KV + throughput (GAP-26/24).

Run: uv run python -m evals.audit_fixes   (needs CF_BASE_URL; offline sections still run)
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import statistics
import time
from pathlib import Path
from typing import Any

import httpx

from evals.harness import Harness, MetricsScraper, OpenAICompatibleClient, count_tokens
from evals.stage_b2 import CARTRIDGE, INSTRUCTION, facts, pointers

REPORTS = Path(__file__).parent / "reports"
REPEATS = int(os.environ.get("CF_REPEATS", "3"))
DISCOUNT = float(os.environ.get("CF_CACHE_DISCOUNT", "0.1"))
Messages = list[dict[str, str]]


# ----------------------------------------------------------------------------- F7 helpers
def synth_repo(n_files: int = 12, funcs_per_file: int = 8, body_lines: int = 12) -> tuple[str, str]:
    """Return (raw_source, extracted_signatures) for a synthetic multi-file repo."""
    raw: list[str] = []
    sig: list[str] = []
    for f in range(n_files):
        raw.append(f"# module_{f}.py")
        for g in range(funcs_per_file):
            raw.append(f"def fn_{f}_{g}(a: int, b: str) -> bool:")
            for ln in range(body_lines):
                raw.append(f"    x_{ln} = a + {ln}  # logic line {ln} of fn_{f}_{g}")
            raw.append("    return True")
        sig.append(
            f"module_{f}: " + ", ".join(f"fn_{f}_{g}(int,str)->bool" for g in range(funcs_per_file))
        )
    return "\n".join(raw), "\n".join(sig)


# --------------------------------------------------------------------------- F4 helpers
def obs_step(i: int, lines: int = 15) -> str:
    # Non-indexed values so the answer cannot be guessed from the key name (retention probe).
    head = f"STEP {i} FACT: key{i} = {VALUES[i]}"
    body = "\n".join(f"  detail {j}: {'y' * 10}" for j in range(lines))
    return f"{head}\n{body}"


_VALS_RNG = random.Random(1337)
VALUES: dict[int, str] = {i: f"v{_VALS_RNG.randint(100000, 999999)}" for i in range(1, 201)}


def fact_for(o: str) -> str:
    return o.splitlines()[0]


def cpp_fold_state(obs_list: list[str], fold_budget: int) -> tuple[list[str], list[str]]:
    ledger: list[str] = []
    live: list[str] = []
    for o in obs_list:
        live.append(o)
        while count_tokens("\n".join(live)) > fold_budget and len(live) > 1:
            ledger.append(fact_for(live.pop(0)))
    return ledger, live


def cartridge(modules: int) -> str:
    return "## PROJECT HOLOGRAM (Tier 0)\n" + "\n".join(
        f"- module_{i}: exports symbol_{i}() used by module_{i + 1}" for i in range(modules)
    )


# ---------------------------------------------------------------------------------- stats
def agg(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "std": 0.0}
    return {
        "mean": statistics.fmean(values),
        "std": statistics.pstdev(values) if len(values) > 1 else 0.0,
    }


# ------------------------------------------------------------------------------- sections
def section_claim_15x(client: OpenAICompatibleClient) -> dict[str, Any]:
    """F8: single request vs N sub-agents carrying the same context."""
    ctx = "\n".join(obs_step(i) for i in range(15))
    single = [
        {"role": "system", "content": INSTRUCTION},
        {"role": "user", "content": f"CONTEXT:\n{ctx}\nTASK: summarize\nNext action?"},
    ]
    single_tokens = count_tokens(single[0]["content"] + single[1]["content"])
    n = 15
    multi_tokens = n * single_tokens
    return {
        "single_chat_tokens": single_tokens,
        "n_agents": n,
        "multi_agent_tokens": multi_tokens,
        "ratio": multi_tokens / single_tokens if single_tokens else 0.0,
    }


def section_compression() -> dict[str, Any]:
    """F7: honest raw->compiled ratio from a deterministic extractor (no LLM)."""
    raw, sig = synth_repo()
    return {
        "raw_tokens": count_tokens(raw),
        "compiled_tokens": count_tokens(sig),
        "ratio": count_tokens(raw) / count_tokens(sig) if count_tokens(sig) else 0.0,
    }


def section_intra_long(client: OpenAICompatibleClient, scraper: MetricsScraper) -> dict[str, Any]:
    """F4: bounded context + early-fact retention over a long horizon."""
    steps = 50
    obs = [obs_step(i) for i in range(1, steps + 1)]
    window = 10
    fold_budget = 900
    out: dict[str, Any] = {"steps": steps, "arms": {}}

    def run(label: str, builder: Any) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for rep in range(REPEATS):
            h = Harness(client, f"{label}_{rep}", scraper=scraper)
            exceeded = False
            for s in range(1, steps + 1):
                try:
                    h.turn(builder(s, obs[:s], rep), max_tokens=8)
                except httpx.HTTPStatusError as e:
                    exceeded = "maximum context length" in str(e)
                    break
            m = h.metrics.summary()
            # retention: ask about the earliest fact (key1). The unbounded arm may overflow.
            try:
                retention = _ask(client, builder(steps, obs[:steps], rep), "What is key1?")
                retained = VALUES[1] in retention
            except httpx.HTTPStatusError:
                retained = False
            results.append(
                {
                    "dynamic": m["dynamic_tokens_total"],
                    "effective": m["dynamic_tokens_total"] + DISCOUNT * m["cached_tokens_total"],
                    "ttft": m["mean_ttft_ms"],
                    "exceeded": exceeded,
                    "retained_key1": retained,
                    "turns": m["turns"],
                }
            )
        return {
            "effective": agg([r["effective"] for r in results]),
            "dynamic": agg([r["dynamic"] for r in results]),
            "ttft": agg([float(r["ttft"]) for r in results]),
            "exceeded_any": any(r["exceeded"] for r in results),
            "retained_key1_frac": sum(r["retained_key1"] for r in results) / len(results),
            "mean_turns": agg([float(r["turns"]) for r in results]),
        }

    def b0_unbounded(step: int, history: list[str], rep: int) -> Messages:
        return [
            {"role": "system", "content": f"REP{rep}\n{INSTRUCTION}"},
            {"role": "user", "content": "\n".join(history)},
        ]

    def b0_window(step: int, history: list[str], rep: int) -> Messages:
        return [
            {"role": "system", "content": f"REP{rep}\n{INSTRUCTION}"},
            {"role": "user", "content": "\n".join(history[-window:])},
        ]

    def cpp_fold(step: int, history: list[str], rep: int) -> Messages:
        ledger, live = cpp_fold_state(history, fold_budget)
        prefix = f"REP{rep}\n{INSTRUCTION}\nLEDGER:\n" + "\n".join(ledger)
        return [{"role": "system", "content": prefix}, {"role": "user", "content": "\n".join(live)}]

    out["arms"]["B0_unbounded"] = run("B0_unbounded", b0_unbounded)
    out["arms"]["B0_window"] = run("B0_window", b0_window)
    out["arms"]["CPP_fold"] = run("CPP_fold", cpp_fold)
    return out


def _ask(client: OpenAICompatibleClient, msgs: Messages, question: str) -> str:
    payload = {
        "model": client.model,
        "messages": msgs + [{"role": "user", "content": question}],
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


def section_fork_fair(client: OpenAICompatibleClient, scraper: MetricsScraper) -> dict[str, Any]:
    """F5: fair fork arms with error bars. Adds adaptive-cartridge CPP_v2."""
    steps, n_children = 15, 8
    raw = "\n".join(obs_step(i) for i in range(steps))
    tasks = [
        f"sub-task {i}: {'audit' if i % 2 else 'implement'} the retrieval path"
        for i in range(n_children)
    ]
    arms: dict[str, list[dict[str, Any]]] = {"B0_top": [], "B0_tail": [], "CPP_v2": []}
    for rep in range(REPEATS):
        tag = f"rep{rep}"
        prefix = f"CASE {tag}\n{cartridge(40)}\n{INSTRUCTION}\n{facts(steps)}\n{pointers(steps)}"
        variants = {
            "B0_top": [f"ROLE/TASK: {t}\nCASE {tag}\n{raw}" for t in tasks],
            "B0_tail": [f"CASE {tag}\n{raw}\nROLE/TASK: {t}" for t in tasks],
            "CPP_v2": [f"ROLE/TASK: {t}\nNext action?" for t in tasks],
        }
        for arm, users in variants.items():
            turns: list[Any] = []
            for u in users:
                msgs = (
                    [{"role": "system", "content": prefix}, {"role": "user", "content": u}]
                    if arm == "CPP_v2"
                    else [
                        {"role": "system", "content": INSTRUCTION},
                        {"role": "user", "content": u},
                    ]
                )
                h = Harness(client, arm, scraper=scraper)
                h.turn(msgs, max_tokens=8)
                turns.extend(h.metrics.turns)
            dyn = sum(t.dynamic_tokens for t in turns)
            cac = sum(t.cached_tokens for t in turns)
            arms[arm].append({"effective": dyn + DISCOUNT * cac, "dynamic": dyn})
    out: dict[str, dict[str, Any]] = {
        arm: {
            "effective": agg([r["effective"] for r in rs]),
            "dynamic": agg([r["dynamic"] for r in rs]),
        }
        for arm, rs in arms.items()
    }
    cpp = out["CPP_v2"]["effective"]["mean"]
    for arm in ("B0_top", "B0_tail"):
        out[arm]["x_vs_cpp"] = (out[arm]["effective"]["mean"] / cpp) if cpp else 0.0
    return out


async def _conc_case(
    arm: str, msgs_list: list[Messages], base_url: str, metrics_url: str
) -> dict[str, Any]:
    samples: list[dict[str, float]] = []
    stop = asyncio.Event()

    async def sampler() -> None:
        async with httpx.AsyncClient(timeout=5.0) as c:
            while not stop.is_set():
                try:
                    r = await c.get(metrics_url)
                    if r.status_code == 200:
                        samples.append(_parse(r.text))
                except Exception:
                    pass
                await asyncio.sleep(0.025)

    task = asyncio.create_task(sampler())

    async def one(m: Messages) -> int:
        async with httpx.AsyncClient(timeout=300.0) as c:
            r = await c.post(
                f"{base_url}/chat/completions",
                json={
                    "model": os.environ.get("CF_MODEL", "Qwen/Qwen2.5-1.5B-Instruct"),
                    "messages": m,
                    "max_tokens": 8,
                    "temperature": 0.0,
                },
                headers={"Authorization": "Bearer EMPTY"},
            )
            r.raise_for_status()
            return int(r.json().get("usage", {}).get("prompt_tokens", 0))

    start = time.perf_counter()
    toks = await asyncio.gather(*(one(m) for m in msgs_list))
    wall = (time.perf_counter() - start) * 1000
    stop.set()
    await task
    return {
        "arm": arm,
        "k": len(msgs_list),
        "wall_ms": wall,
        "req_per_s": len(msgs_list) / (wall / 1000),
        "peak_kv_perc": max((s["kv_cache_usage_perc"] for s in samples), default=0.0),
        "peak_waiting": max((s["num_requests_waiting"] for s in samples), default=0.0),
        "prompt_tokens": sum(toks),
    }


def _parse(text: str) -> dict[str, float]:
    out = {"kv_cache_usage_perc": 0.0, "num_requests_running": 0.0, "num_requests_waiting": 0.0}
    for line in text.splitlines():
        if not line.startswith("vllm:"):
            continue
        head, _, val = line.rpartition(" ")
        key = head.split("{")[0].split(":", 1)[1]
        if key in out:
            try:
                out[key] = float(val)
            except ValueError:
                pass
    return out


def section_concurrency(base_url: str, metrics_url: str) -> list[dict[str, Any]]:
    """F6: add B0_tail (shared raw prefix) to isolate sharing vs compression."""
    k = 16
    raw = "\n".join(obs_step(i) for i in range(15))
    prefix = f"{CARTRIDGE}\n{INSTRUCTION}\n{facts(15)}\n{pointers(15)}"
    arms = {
        "B0_divergent": [
            [
                {"role": "system", "content": INSTRUCTION},
                {"role": "user", "content": f"CHILD {i}\n{raw}"},
            ]
            for i in range(k)
        ],
        "B0_tail_shared": [
            [
                {"role": "system", "content": INSTRUCTION},
                {"role": "user", "content": f"{raw}\nTASK {i}"},
            ]
            for i in range(k)
        ],
        "CPP_shared": [
            [{"role": "system", "content": prefix}, {"role": "user", "content": f"TASK {i}"}]
            for i in range(k)
        ],
    }
    return [asyncio.run(_conc_case(a, m, base_url, metrics_url)) for a, m in arms.items()]


def main() -> int:
    base_url = os.environ.get("CF_BASE_URL", "http://localhost:8000/v1")
    model = os.environ.get("CF_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
    metrics_url = base_url.rsplit("/v1", 1)[0] + "/metrics"
    report: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "repeats": REPEATS,
    }
    want = set(os.environ.get("CF_SECTION", "all").split(","))

    def on(name: str) -> bool:
        return "all" in want or name in want

    if on("compression"):
        report["compression"] = section_compression()  # offline

    try:
        up = httpx.get(f"{base_url.rstrip('/')}/models", timeout=3.0).status_code < 500
    except Exception:
        up = False
    if not up:
        print(
            "Offline: compression only. Start an endpoint for the rest "
            "(scripts/wsl_run_eval.sh evals.audit_fixes)."
        )
        print(json.dumps(report, indent=2))
        return 0

    client = OpenAICompatibleClient(base_url=base_url, model=model, api_key="EMPTY")
    scraper = MetricsScraper(metrics_url)
    try:
        if on("claim"):
            report["claim_15x"] = section_claim_15x(client)
        if on("intra"):
            report["intra_long"] = section_intra_long(client, scraper)
        if on("fork"):
            report["fork_fair"] = section_fork_fair(client, scraper)
        if on("concurrency"):
            report["concurrency"] = section_concurrency(base_url, metrics_url)
    finally:
        client.close()

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"audit_fixes_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nReport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
