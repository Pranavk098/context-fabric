"""Offline re-scoring of stored negative-knowledge responses under scorer v1/v2/v3.

No GPU, no network, no model calls: reads the stored ``evals/reports/demo_negative*.json``
receipts, re-applies all three scorers to each stored ``response_text`` (the same function
for every arm), writes ``evals/reports/negscore_compare_<ts>.json``, and prints per
model/op/arm/trap raw counts. The trap keyword lists come from
``sidecar.demo.negative_candidates`` (the pre-registered trap spec); records without a
response (``op: write``) are skipped. Responses come from the receipts' generation runs;
this script only re-scores them and verifies the stored verdicts reproduce.

Run: ``uv run python -m evals.negscore_compare``
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sidecar.demo import negative_candidates
from sidecar.negscore import score_v1, score_v2, score_v3

REPORTS = Path(__file__).parent / "reports"


def trap_terms() -> dict[str, tuple[str, ...]]:
    return {trap.trap_id: trap.bad_keywords for trap in negative_candidates()}


def rescore(report_paths: list[Path]) -> list[dict[str, Any]]:
    terms = trap_terms()
    rows: list[dict[str, Any]] = []
    for path in report_paths:
        receipt: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        model = str(receipt.get("model", path.stem))
        for record in receipt.get("records", []):
            text = record.get("response_text")
            trap = record.get("trap")
            if record.get("op") == "write" or not text or trap not in terms:
                continue
            bad_terms = terms[trap]
            v1 = score_v1(text, bad_terms)
            v2 = score_v2(text, bad_terms)
            v3 = score_v3(text, bad_terms)
            rows.append(
                {
                    "report": path.name,
                    "model": model,
                    "op": record.get("op"),
                    "arm": record.get("arm"),
                    "trap": trap,
                    "repeat": record.get("repeat"),
                    "stored_bad_v1": record.get("bad"),
                    "stored_bad_v2": record.get("bad_v2"),
                    "stored_bad_v3": record.get("bad_v3"),
                    "v1": {"bad": v1.bad, "evidence": v1.evidence},
                    "v2": {"bad": v2.bad, "evidence": v2.evidence},
                    "v3": {"bad": v3.bad, "evidence": v3.evidence},
                }
            )
    return rows


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    index: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = (row["model"], row["op"], row["arm"], row["trap"])
        entry = index.get(key)
        if entry is None:
            entry = {
                "model": row["model"],
                "op": row["op"],
                "arm": row["arm"],
                "trap": row["trap"],
                "n": 0,
                "v1_bad": 0,
                "v2_bad": 0,
                "v3_bad": 0,
            }
            index[key] = entry
            summary.append(entry)
        entry["n"] += 1
        for scorer in ("v1", "v2", "v3"):
            if row[scorer]["bad"]:
                entry[f"{scorer}_bad"] += 1
    return summary


def _print_table(summary: list[dict[str, Any]]) -> None:
    header = (
        f"{'model':<22} {'op':<5} {'arm':<8} {'trap':<8} {'n':>2} {'v1':>4} {'v2':>4} {'v3':>4}"
    )
    print(header)
    print("-" * len(header))
    for entry in summary:
        model = str(entry["model"]).split("/")[-1]
        print(
            f"{model:<22} {entry['op']:<5} {entry['arm']:<8} {entry['trap']:<8}"
            f" {entry['n']:>2} {entry['v1_bad']:>4} {entry['v2_bad']:>4} {entry['v3_bad']:>4}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reports",
        nargs="*",
        type=Path,
        default=None,
        help="negative receipt JSONs (default: evals/reports/demo_negative*.json)",
    )
    args = parser.parse_args()
    paths: list[Path] = args.reports or sorted(REPORTS.glob("demo_negative*.json"))
    rows = rescore(paths)
    summary = summarize(rows)
    mismatches = [
        {
            "report": row["report"],
            "model": row["model"],
            "op": row["op"],
            "arm": row["arm"],
            "trap": row["trap"],
            "repeat": row["repeat"],
            "stored_bad_v1": row["stored_bad_v1"],
            "rescored_bad_v1": row["v1"]["bad"],
            "stored_bad_v2": row["stored_bad_v2"],
            "rescored_bad_v2": row["v2"]["bad"],
            "stored_bad_v3": row["stored_bad_v3"],
            "rescored_bad_v3": row["v3"]["bad"],
        }
        for row in rows
        if (
            row["stored_bad_v1"] != row["v1"]["bad"]
            or row["stored_bad_v2"] != row["v2"]["bad"]
            or (row["stored_bad_v3"] is not None and row["stored_bad_v3"] != row["v3"]["bad"])
        )
    ]

    _print_table(summary)
    flips = [row for row in rows if row["v2"]["bad"] != row["v3"]["bad"]]
    if flips:
        print("\nv2 -> v3 flips (stored response_text):")
        for row in flips:
            model = str(row["model"]).split("/")[-1]
            verdict = f"v2={row['v2']['bad']} v3={row['v3']['bad']}"
            print(f"  {model} {row['op']} {row['arm']} {row['trap']} #{row['repeat']}: {verdict}")
            for item in row["v3"]["evidence"]:
                print(f"    v3 evidence: {item}")
    if mismatches:
        print(f"\nWARNING: {len(mismatches)} stored-verdict mismatch(es); see the JSON")
    else:
        print("\nstored v1/v2/v3 verdicts reproduce exactly from response_text")

    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "scorers": ["v1", "v2", "v3"],
        "reports": [path.name for path in paths],
        "notes": (
            "Offline re-scoring of stored response_text (first 500 chars) under scorer "
            "v1/v2/v3; no model calls — responses come from the receipts' generation runs. "
            "Scorer v3 is pre-registered in docs/engineering/NEGATIVE-SCORER.md."
        ),
        "records": rows,
        "summary": summary,
        "stored_verdict_mismatches": mismatches,
    }
    out = REPORTS / f"negscore_compare_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
