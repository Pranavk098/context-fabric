"""v4 analyzer (WP H2): primary and secondary tables from fake run records."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from evals.plugin_ab import analyze

# arm -> (cost multiplier, re-reads, compactions, pass, planted facts passed of 2, spawned)
_ARM_SHAPE: dict[str, tuple[float, int, int, bool, int, bool]] = {
    "none": (1.0, 3, 0, True, 2, True),
    "brief_v0": (0.95, 2, 0, True, 2, True),
    "brief_v1": (0.80, 0, 0, True, 2, True),
    "fork": (1.60, 0, 1, True, 2, True),
    "control": (1.0, 3, 0, False, 0, True),
}


def _records() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for task_index, base in enumerate((0.10, 0.20, 0.40)):
        for rep in range(2):
            for arm, (mult, rereads, comp, ok, facts, spawned) in _ARM_SHAPE.items():
                out.append(
                    {
                        "v4": True,
                        "task": f"t{task_index}",
                        "arm": arm,
                        "rep": rep,
                        "warm_up": False,
                        "dollars": base * mult,
                        "resolved": ok,
                        "hidden_pass": True,
                        "planted_passed": facts,
                        "planted_of": 2,
                        "no_spawn": not spawned,
                        "child_rereads": rereads,
                        "child_compactions": comp,
                        "child_overflows": 0,
                        "child_parent_msg_overlap": 4 if arm == "fork" else 0,
                        "cap_hit": False,
                        "mode": "pilot",
                    }
                )
    out.append({"v4": True, "task": "t0", "arm": "none", "infra_failure": True})
    out.append({"task": "old", "arm": "off", "dollars": 9.0, "resolved": True})  # not v4
    return out


def test_primary_is_brief_v1_vs_none_with_the_v1_bootstrap() -> None:
    result = analyze.analyze_v4(_records(), resamples=500)
    p = result["primary"]
    assert (p["treatment"], p["baseline"]) == ("brief_v1", "none")
    assert p["tasks"] == 3
    assert p["geometric_mean_cost_ratio"] == 0.8
    lo, hi = p["bootstrap_95ci"]
    assert lo <= 0.8 <= hi and math.isclose(lo, hi)  # identical ratios on every task
    assert p["treatment_pass_rate"] == p["baseline_pass_rate"] == 1.0
    assert p["non_inferior"] is True
    assert result["infra_excluded"] == 1 and result["pilot"] is True


def test_secondary_comparisons_and_arm_table() -> None:
    result = analyze.analyze_v4(_records(), resamples=200)
    sec = {c["baseline"]: c for c in result["secondary"]}
    assert set(sec) == {"fork", "brief_v0"}
    assert sec["fork"]["geometric_mean_cost_ratio"] == 0.5
    assert sec["fork"]["delta_child_compactions"] == -1.0
    assert sec["brief_v0"]["delta_child_rereads"] == -2.0
    arms = result["arms"]
    assert arms["none"]["runs"] == 6  # the infra failure is excluded
    assert arms["brief_v1"]["mean_child_rereads"] == 0.0
    assert arms["fork"]["mean_child_parent_msg_overlap"] == 4.0
    assert arms["fork"]["spawn_rate"] == 1.0


def test_control_must_fail_planted_facts_to_be_valid() -> None:
    result = analyze.analyze_v4(_records(), resamples=50)
    assert result["control_valid"] is True
    assert result["arms"]["control"]["planted_fact_pass_rate"] == 0.0
    leaky = [{**r, "planted_passed": 1} if r.get("arm") == "control" else r for r in _records()]
    assert analyze.analyze_v4(leaky, resamples=50)["control_valid"] is False


def test_no_spawn_lowers_spawn_rate_and_is_kept_in_the_data() -> None:
    records = _records()
    for r in records:
        if r.get("arm") == "none" and r.get("rep") == 0 and r.get("task") == "t0":
            r["no_spawn"] = True
    arm = analyze.analyze_v4(records, resamples=50)["arms"]["none"]
    assert arm["no_spawn_runs"] == 1 and arm["runs"] == 6
    assert math.isclose(arm["spawn_rate"], 5 / 6, abs_tol=1e-4)


def test_non_inferiority_flags_a_task_lost_to_the_baseline() -> None:
    records = _records()
    for r in records:
        if r.get("arm") == "brief_v1" and r.get("task") in ("t0", "t1"):
            r["resolved"] = False
    p = analyze.analyze_v4(records, resamples=50)["primary"]
    assert p["tasks_treatment_fails_baseline_passes"] == 2 and p["non_inferior"] is False


def test_markdown_has_primary_and_secondary_tables_and_no_claim() -> None:
    text = analyze.format_markdown_v4(analyze.analyze_v4(_records(), resamples=100))
    assert "## Primary: total cost (parent + children) at non-inferior pass rate" in text
    assert "| brief_v1 vs none | 3 | 0.800 |" in text
    assert "## Secondary: comparisons" in text and "brief_v1 vs fork" in text
    assert "brief_v1 vs brief_v0" in text
    assert "## Secondary: per arm" in text
    for arm in analyze.V4_ARMS:
        assert f"| {arm} |" in text
    assert "Control check" in text and "valid" in text
    assert "PILOT (not evidence)" in text
    assert "significant" not in text.lower() and "claim" not in text.lower()


def test_cli_writes_v4_reports(tmp_path: Path, capsys: Any) -> None:
    runs = [{k: v for k, v in r.items() if k != "mode"} for r in _records()]
    (tmp_path / "r.json").write_text(json.dumps({"mode": "pilot", "runs": runs}), encoding="utf-8")
    assert analyze.main(["--v4", "--report-dir", str(tmp_path)]) == 0
    assert "## Primary" in capsys.readouterr().out
    assert (tmp_path / "analysis_v4.md").is_file()
    data = json.loads((tmp_path / "analysis_v4.json").read_text(encoding="utf-8"))
    assert data["primary"]["treatment"] == "brief_v1"


def test_empty_input_is_all_n_a() -> None:
    result = analyze.analyze_v4([], resamples=10)
    assert result["primary"]["geometric_mean_cost_ratio"] is None
    assert result["control_valid"] is None
    assert "n/a" in analyze.format_markdown_v4(result)
