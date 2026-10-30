"""Recompute the v2 registered ratios from the raw transcripts (EXPLORATORY).

Counting is the analyzer's own: ``evals.plugin_ab.usage.scan_transcript`` (usage counted
once per ``message.id``) and ``evals.plugin_ab.analyze.analyze_v2`` for ratios and CIs.
Only token counts are read from the transcripts. No model is called.

Three accountings per run:

* ``registered``: the run records exactly as written (per-line usage; for checkpoint
  runs only the last session survives the runner's step deltas, see the deviation).
* ``dedup_final``: once-per-message.id, the record's final ``session_id`` only (the
  runner's own scope, so this isolates the double-count fix).
* ``dedup_all``: once-per-message.id, every session the run wrote (checkpoint runs
  start a fresh session at each boundary; all of them are paid for).

Usage:
    uv run python evals/reports/v2_registered_dedup/recompute.py \
        --records C:/dev/cf-bench/evals/reports/v2_registered \
        --projects ~/.claude/projects
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

from evals.plugin_ab import analyze as A
from evals.plugin_ab.prices import load_prices
from evals.plugin_ab.runner import count_mcp_attachments, find_subagent_transcripts
from evals.plugin_ab.usage import ScanResult, combine, scan_transcript

OUT_DIR = Path(__file__).resolve().parent
ZERO = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}


def _block(r: ScanResult) -> dict[str, int]:
    return {
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "cache_creation_input_tokens": r.cache_creation_input_tokens,
        "cache_read_input_tokens": r.cache_read_input_tokens,
    }


def _session_usage(path: Path, prices: dict[str, Any]) -> tuple[ScanResult, ScanResult]:
    subs = find_subagent_transcripts(path, path.stem)
    return scan_transcript(path, prices), combine([scan_transcript(p, prices) for p in subs])


def _with_usage(record: dict[str, Any], parent: ScanResult, subs: ScanResult) -> dict[str, Any]:
    """Copy of the record whose cost is the given totals (pricing is linear per tier;
    the claim tables are single-tier). Step pass/recall fields are kept."""
    out = copy.deepcopy(record)
    steps = out.get("steps")
    total = {"parent": _block(parent), "subagents": _block(subs)}
    if isinstance(steps, list) and steps:
        for i, step in enumerate(steps):
            step["usage"] = total if i == 0 else {"parent": ZERO, "subagents": ZERO}
    else:
        out["usage"] = total
    return out


def _ratios(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = A.analyze_v2(records)
    table: dict[str, dict[str, Any]] = {}
    for stratum, body in result["strata"].items():
        table[stratum] = {
            arm: {
                "ratio": e["geometric_mean_ratio"],
                "ci": e["bootstrap_95ci"],
                "sonnet": e["repriced"]["sonnet-5.5"],
            }
            for arm, e in body["arms"].items()
        }
    return table


def _vs_shipped(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """checkpoint vs shipped: relabel shipped as the reference arm, drop real off."""
    relabelled = []
    for r in records:
        if r.get("arm") == "off":
            continue
        r = dict(r)
        if r["arm"] == "shipped":
            r["arm"] = "off"
        relabelled.append(r)
    return {s: v.get("checkpoint", {}) for s, v in _ratios(relabelled).items()}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--records", required=True, type=Path)
    p.add_argument("--projects", default=str(Path.home() / ".claude" / "projects"), type=Path)
    args = p.parse_args()
    prices = load_prices()
    for model in A.V2_REPRICE_MODELS:
        key = model.replace("claude-", "").replace("-5-5", "-5.5")
        assert len(prices["models"][key]) == 1, f"{key} is tiered; totals would misprice"

    registered = [r for r in A.load_records(args.records) if not A._is_slice0(r)]
    index = {p.stem: p for p in Path(args.projects).glob("*plugin-ab-*/*.jsonl")}
    dedup_final: list[dict[str, Any]] = []
    dedup_all: list[dict[str, Any]] = []
    missing: list[str] = []
    runs: list[dict[str, Any]] = []
    mcp: dict[str, dict[str, list[int]]] = {}
    for r in registered:
        sid = r.get("session_id")
        path = index.get(sid) if isinstance(sid, str) else None
        tag = f"{r.get('stratum')}/{r.get('task')}/{r.get('arm')}/rep{r.get('rep')}"
        if path is None:
            missing.append(tag)
            continue
        fp, fs = _session_usage(path, prices)
        sessions = sorted(path.parent.glob("*.jsonl"))
        scans = [_session_usage(s, prices) for s in sessions]
        ap, as_ = combine([s[0] for s in scans]), combine([s[1] for s in scans])
        dedup_final.append(_with_usage(r, fp, fs))
        dedup_all.append(_with_usage(r, ap, as_))
        deferred = sum(count_mcp_attachments(s)[0] for s in sessions)
        if not r.get("warm_up") and not r.get("infra_failure"):
            mcp.setdefault(str(r["stratum"]), {}).setdefault(str(r["arm"]), []).append(deferred)
        runs.append(
            {
                "run": tag,
                "warm_up": bool(r.get("warm_up")),
                "sessions": len(sessions),
                "deferred_tools_delta": deferred,
                "weighted_registered": r.get("weighted_tokens"),
                "weighted_dedup_final": round(combine([fp, fs]).weighted(), 2),
                "weighted_dedup_all": round(combine([ap, as_]).weighted(), 2),
            }
        )

    result = {
        "label": "EXPLORATORY: not a registered result",
        "records_dir": str(args.records),
        "runs_in_records": len(registered),
        "runs_missing_transcripts": missing,
        "vs_off": {
            "registered": _ratios(registered),
            "dedup_final": _ratios(dedup_final),
            "dedup_all": _ratios(dedup_all),
        },
        "checkpoint_vs_shipped": {
            "registered": _vs_shipped(registered),
            "dedup_final": _vs_shipped(dedup_final),
            "dedup_all": _vs_shipped(dedup_all),
        },
        "deferred_tools_delta_per_run": {
            s: {a: v for a, v in arms.items()} for s, arms in mcp.items()
        },
        "runs": runs,
    }
    (OUT_DIR / "results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    md = _markdown(result)
    (OUT_DIR / "results.md").write_text(md, encoding="utf-8")
    sys.stdout.write(md)
    return 0


def _fmt(e: dict[str, Any]) -> str:
    if not e or e.get("ratio") is None:
        return "n/a"
    ci = e.get("ci")
    return f"{e['ratio']:.3f} ({ci[0]:.3f}-{ci[1]:.3f})" if ci else f"{e['ratio']:.3f}"


def _markdown(res: dict[str, Any]) -> str:
    lines = [
        "# v2 registered runs, recomputed with once-per-message.id usage (EXPLORATORY)",
        "",
        f"Runs in records: {res['runs_in_records']}. "
        f"Runs with missing transcripts: {len(res['runs_missing_transcripts'])}.",
        "",
        "Ratio = geometric mean over tasks of arm/reference mean cost, priced as Opus 5.5,"
        " with the registered hierarchical bootstrap 95% CI.",
        "",
        "## Arm vs off (registered off-based ratios are VOID)",
        "",
        "| stratum | arm | registered | dedup, final session | dedup, all sessions |",
        "| --- | --- | --- | --- | --- |",
    ]
    vo = res["vs_off"]
    for s in A.STRATA:
        for arm in ("shipped", "checkpoint"):
            cells = [_fmt(vo[k].get(s, {}).get(arm, {})) for k in vo]
            lines.append(f"| {s} | {arm} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "## Checkpoint vs shipped (reported, not claimed)",
        "",
        "| stratum | registered | dedup, final session | dedup, all sessions |",
        "| --- | --- | --- | --- |",
    ]
    cs = res["checkpoint_vs_shipped"]
    for s in A.STRATA:
        lines.append(f"| {s} | " + " | ".join(_fmt(cs[k].get(s, {})) for k in cs) + " |")
    lines += [
        "",
        "## deferred_tools_delta attachments per measured run (all sessions of the run)",
        "",
        "| stratum | arm | per-run counts |",
        "| --- | --- | --- |",
    ]
    for s, arms in res["deferred_tools_delta_per_run"].items():
        for arm, counts in arms.items():
            lines.append(f"| {s} | {arm} | {counts} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
