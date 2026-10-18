"""GPU position probe — does WHERE a critical rule sits in the prompt change adherence?

One critical rule ("the gateway listens on port 7443; always include 7443 in any port
guidance") is embedded in a ~3k-token deterministic filler context at three positions —
HEAD (first ~10%), MIDDLE (~50%), TAIL (last ~10%) — and the same 8 probe questions ask
for port guidance whose only correct answer is 7443 (n = 8 per position, temperature 0).

Deterministic scorer (simple and symmetric): a response is a success when it contains
7443 and no other 2-5 digit number; a failure is either "did not mention 7443" or "stated
a different port". The number filter is deliberately crude (any other number counts as a
different port claim) and documented as such.

Run under WSL via the shared boot recipe (one vLLM session, enforce-eager):

    wsl -d Ubuntu -- bash /mnt/c/dev/Context-Fabric/scripts/wsl_run_eval.sh evals.position_probe

Writes ``evals/reports/position_probe_<ts>.json`` (per-probe rows + per-position counts).
A null result is reported as-is; prompts are never tuned after the run to manufacture an
effect.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from evals.harness import OpenAICompatibleClient, count_tokens

REPORTS = Path(__file__).parent / "reports"

RULE = (
    "Operations rule: the gateway listens on port 7443; always include 7443 in any port guidance."
)

SYSTEM = "You are an operations assistant. Answer using only the provided context."

PROBES = (
    "A new service must call the gateway. In one sentence, which port should it use?",
    "Write a one-line onboarding note for engineers on which port the gateway uses.",
    "What port should deployment docs mention for gateway traffic? One short sentence.",
    "Give the port that service teams must use to reach the gateway.",
    "A teammate asks which gateway port to configure. Answer briefly.",
    "State the gateway port for the runbook, in one sentence.",
    "Which port belongs in the gateway section of the integration guide?",
    "Give one sentence of port guidance for calling the gateway.",
)

POSITIONS = ("head", "middle", "tail")

_SERVICES = (
    "ingest",
    "indexer",
    "gateway",
    "scheduler",
    "reconciler",
    "notifier",
    "exporter",
    "auditor",
    "archiver",
    "profiler",
)

_NUMBER = re.compile(r"\d{2,5}")


def _sections() -> list[str]:
    """Deterministic filler; one long sentence per section, no ports, no RNG."""
    return [
        f"Section {i:02d}: the {_SERVICES[i % len(_SERVICES)]} pipeline processes batch "
        f"{i} of the rollout and the on-call rotation for shard {i % 7} reviews watermark "
        f"lag and backlog drains during the review window before promoting the next "
        f"batch; the reviewer records the outcome on the {('core', 'platform', 'sre')[i % 3]} "
        f"board and schedules follow-up checks for the {i % 5} pending migration. "
        f"Window retention is {7 + i % 14} days and the "
        f"{_SERVICES[(i + 3) % len(_SERVICES)]} owner signs off on the drain report "
        f"before the rollout advances to the next checkpoint in the quarterly plan; "
        f"deviations are logged with the batch id and replayed during the next "
        f"dry-run cycle."
        for i in range(1, 31)
    ]


def contexts() -> dict[str, str]:
    """The same filler with the rule at HEAD, MIDDLE (~50%), or TAIL of the context."""
    sections = _sections()
    head = [RULE, *sections]
    middle = [*sections[:15], RULE, *sections[15:]]
    tail = [*sections, RULE]
    return {
        "head": "\n".join(head),
        "middle": "\n".join(middle),
        "tail": "\n".join(tail),
    }


def score_response(text: str) -> tuple[bool, list[str]]:
    """Success = mentions 7443 and claims no other 2-5 digit number."""
    numbers = _NUMBER.findall(text)
    other = sorted({n for n in numbers if n != "7443"})
    return ("7443" in numbers and not other, other)


def run(client: OpenAICompatibleClient, max_tokens: int = 64) -> dict[str, Any]:
    ctx = contexts()
    rows: list[dict[str, Any]] = []
    for position in POSITIONS:
        context = ctx[position]
        for probe in PROBES:
            response = client.chat(
                [
                    {"role": "system", "content": f"{SYSTEM}\n\nCONTEXT:\n{context}"},
                    {"role": "user", "content": probe},
                ],
                max_tokens=max_tokens,
            )
            ok, other = score_response(response["text"])
            rows.append(
                {
                    "position": position,
                    "probe": probe,
                    "response": response["text"],
                    "has_7443": "7443" in response["text"],
                    "other_numbers": other,
                    "ok": ok,
                    "prompt_tokens": response["prompt_tokens"],
                }
            )
    positions = {
        position: {
            "success": sum(1 for r in rows if r["position"] == position and r["ok"]),
            "total": sum(1 for r in rows if r["position"] == position),
            "fail_missing_7443": sum(
                1 for r in rows if r["position"] == position and "7443" not in r["response"]
            ),
            "fail_other_number": sum(
                1 for r in rows if r["position"] == position and r["other_numbers"]
            ),
        }
        for position in POSITIONS
    }
    context_tokens = {position: count_tokens(text) for position, text in ctx.items()}
    assert len(set(context_tokens.values())) == 1, "positions must not change the token count"
    return {
        "scenario": "position_probe",
        "model": client.model,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "rule": RULE,
        "context_tokens": context_tokens["head"],
        "positions": positions,
        "rows": rows,
        "notes": [
            "Synthetic deterministic filler; the ONLY route to 7443 is the embedded rule.",
            "Scorer: success = contains 7443 and no other 2-5 digit number; any other "
            "number is counted as a different port claim (crude by design, symmetric).",
            "n = 8 probes per position; temperature 0.0; max_tokens=64; one model.",
            "A null result (no position effect) is a valid outcome and is reported as-is.",
        ],
    }


def main() -> int:
    base_url = os.environ.get("CF_BASE_URL", "http://localhost:8000/v1")
    model = os.environ.get("CF_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
    client = OpenAICompatibleClient(base_url=base_url, model=model, api_key="EMPTY")
    try:
        report = run(client)
    finally:
        client.close()
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = REPORTS / f"position_probe_{ts}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"position probe — {report['model']} (context {report['context_tokens']} tokens)")
    print(f"{'position':<9}{'success':>9}{'missing':>9}{'other_port':>12}")
    for position, stats in report["positions"].items():
        print(
            f"{position:<9}{stats['success']:>6}/{stats['total']:<2}"
            f"{stats['fail_missing_7443']:>9}{stats['fail_other_number']:>12}"
        )
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
