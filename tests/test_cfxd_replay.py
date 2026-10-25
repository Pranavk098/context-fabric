"""cfxd replay tests: offline shrink simulation and cache-expiry measurement."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cfxd.replay import format_replay, replay

ROOT = Path(__file__).resolve().parents[1]


def _usage(input_tokens: int = 0, output_tokens: int = 0, cc: int = 0, cr: int = 0) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cc,
        "cache_read_input_tokens": cr,
    }


def _assistant(*blocks: dict, usage: dict | None = None, ts: str | None = None) -> dict:
    obj = {"type": "assistant", "message": {"content": list(blocks), "usage": usage or {}}}
    if ts is not None:
        obj["timestamp"] = ts
    return obj


def _user(*blocks: dict) -> dict:
    return {"type": "user", "message": {"content": list(blocks)}}


def _tool_use(tool_id: str, name: str = "Bash") -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {}}


def _tool_result(tool_id: str, content: object) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_id, "content": content}


def _session(root: Path, project: str, session_id: str, lines: list[dict]) -> Path:
    path = root / project / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _variant(report: dict, k: int, batch: int, variant: str, r: float) -> dict:
    for policy in report["policies"]:
        if policy["k"] == k and policy["batch"] == batch:
            for v in policy["variants"]:
                if v["variant"] == variant and v["r"] == r:
                    return v
    raise AssertionError(f"variant {variant} r={r} not found for k={k} batch={batch}")


def _twelve_turn_lines() -> list[dict]:
    """12 assistant turns; result R_i (40 bytes) enters at turn i+1.

    Usage: input=10, output=1, cc=0, cr=10*t -> weighted = 15 + t, total 246.
    """
    lines: list[dict] = []
    for t in range(12):
        usage = _usage(10, 1, 0, 10 * t)
        if t < 11:
            lines.append(_assistant(_tool_use(f"t{t}"), usage=usage))
            lines.append(_user(_tool_result(f"t{t}", "x" * 40)))
        else:
            lines.append(_assistant(usage=usage))
    return lines


def test_no_results_over_min_bytes(tmp_path: Path) -> None:
    lines = [
        _assistant(_tool_use("a"), usage=_usage(10, 1, 0, 10)),
        _user(_tool_result("a", "x" * 4)),
        _assistant(_tool_use("b"), usage=_usage(10, 1, 0, 20)),
        _user(_tool_result("b", "y" * 4)),
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = replay(tmp_path, "all", ks=(3,), batches=(1,), min_bytes=8, stub_bytes=4)
    assert report["sessions"] == 1
    assert report["baseline_weighted"] > 0
    for policy in report["policies"]:
        for v in policy["variants"]:
            assert v["pct_saved"] == 0.0
            assert v["pct_saved_over_30"] == 0.0


def test_12_turn_hand_computed(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", _twelve_turn_lines())
    report = replay(tmp_path, "all", ks=(3,), batches=(1,), min_bytes=8, stub_bytes=4)

    assert report["sessions"] == 1
    assert report["baseline_weighted"] == pytest.approx(246.0, abs=0.1)

    cons = _variant(report, 3, 1, "conservative", 0.0)
    opt = _variant(report, 3, 1, "optimistic", 0.0)
    # conservative: re-creation penalty dominates -> cost above baseline
    assert cons["simulated_weighted"] == pytest.approx(840.65, abs=0.15)
    assert cons["pct_saved"] == pytest.approx(-241.7, abs=0.15)
    # optimistic: pure 0.1 * removed per later turn -> 25.2 saved out of 246
    assert opt["simulated_weighted"] == pytest.approx(220.8, abs=0.15)
    assert opt["pct_saved"] == pytest.approx(10.2, abs=0.15)

    # re-fetch sensitivity: more re-read => higher (less-saving) cost, monotonic
    by_r = {v["r"]: v["simulated_weighted"] for v in _all_variants(report, 3, 1, "conservative")}
    assert by_r[0.1] > by_r[0.0]
    assert by_r[0.3] > by_r[0.1]


def _all_variants(report: dict, k: int, batch: int, variant: str) -> list[dict]:
    for policy in report["policies"]:
        if policy["k"] == k and policy["batch"] == batch:
            return [v for v in policy["variants"] if v["variant"] == variant]
    return []


def test_batch_5_differs(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", _twelve_turn_lines())
    report = replay(tmp_path, "all", ks=(3,), batches=(1, 5), min_bytes=8, stub_bytes=4)

    b1_cons = _variant(report, 3, 1, "conservative", 0.0)
    b5_cons = _variant(report, 3, 5, "conservative", 0.0)
    b1_opt = _variant(report, 3, 1, "optimistic", 0.0)
    b5_opt = _variant(report, 3, 5, "optimistic", 0.0)

    assert b1_cons["simulated_weighted"] == pytest.approx(840.65, abs=0.15)
    assert b5_cons["simulated_weighted"] == pytest.approx(402.05, abs=0.15)
    assert b1_opt["simulated_weighted"] == pytest.approx(220.8, abs=0.15)
    assert b5_opt["simulated_weighted"] == pytest.approx(230.7, abs=0.15)
    assert b1_cons["simulated_weighted"] != b5_cons["simulated_weighted"]


def _seven_turn_lines(with_compaction: bool) -> list[dict]:
    lines: list[dict] = []
    for t in range(7):
        usage = _usage(10, 1, 0, 10 * t)
        if t < 6:
            lines.append(_assistant(_tool_use(f"t{t}"), usage=usage))
            if with_compaction and t == 5:
                lines.append({"type": "system", "subtype": "compact_boundary"})
            lines.append(_user(_tool_result(f"t{t}", "x" * 40)))
        else:
            lines.append(_assistant(usage=usage))
    return lines


def test_compaction_resets(tmp_path: Path) -> None:
    for name, with_c, expected in (("with_c", True, 182.6), ("no_c", False, 248.65)):
        d = tmp_path / name
        _session(d, "proj", "s1", _seven_turn_lines(with_compaction=with_c))
        r = replay(d, "all", ks=(3,), batches=(1,), min_bytes=8, stub_bytes=4)
        v = _variant(r, 3, 1, "conservative", 0.0)
        assert v["simulated_weighted"] == pytest.approx(expected, abs=0.15), name


def test_cache_expiry_classes(tmp_path: Path) -> None:
    lines = [
        _assistant(usage=_usage(0, 0, 100, 0), ts="2024-01-01T00:00:00Z"),
        _assistant(usage=_usage(0, 0, 200, 0), ts="2024-01-01T00:01:00Z"),
        _assistant(usage=_usage(0, 0, 300, 100), ts="2024-01-01T00:11:00Z"),
        _assistant(usage=_usage(0, 0, 400, 0), ts="2024-01-01T02:11:00Z"),
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = replay(tmp_path, "all", ks=(3,), batches=(1,))

    ce = report["cache_expiry"]
    assert ce["<=5m"]["turns"] == 1
    assert ce["<=5m"]["cc_share"] == pytest.approx(0.2, abs=1e-4)
    assert ce["<=5m"]["weighted_share"] == pytest.approx(250 / 1260, abs=1e-4)
    assert ce["<=5m"]["cold_cc_share"] == pytest.approx(0.2, abs=1e-4)

    assert ce["5-60m"]["turns"] == 1
    assert ce["5-60m"]["cc_share"] == pytest.approx(0.3, abs=1e-4)
    assert ce["5-60m"]["weighted_share"] == pytest.approx(385 / 1260, abs=1e-4)
    assert ce["5-60m"]["cold_cc_share"] == 0.0

    assert ce[">60m"]["turns"] == 1
    assert ce[">60m"]["cc_share"] == pytest.approx(0.4, abs=1e-4)
    assert ce[">60m"]["weighted_share"] == pytest.approx(500 / 1260, abs=1e-4)
    assert ce[">60m"]["cold_cc_share"] == pytest.approx(0.4, abs=1e-4)


def test_sessions_over_30(tmp_path: Path) -> None:
    lines = [_assistant(usage=_usage(1, 0)) for _ in range(31)]
    _session(tmp_path, "proj", "long", lines)
    report = replay(tmp_path, "all", ks=(3,), batches=(1,), min_bytes=8, stub_bytes=4)
    assert report["sessions"] == 1
    assert report["sessions_over_30"] == 1
    for policy in report["policies"]:
        for v in policy["variants"]:
            assert v["pct_saved"] == 0.0
            assert v["pct_saved_over_30"] == 0.0


def test_empty_root(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    report = replay(empty, "all")
    assert report["sessions"] == 0
    assert format_replay(report, empty.name) == "No transcripts found under empty."


def test_canary_strings_never_leak(tmp_path: Path) -> None:
    path_canary = "CANARYPATH-abcd1234"
    cmd_canary = "CANARYCMD-efgh5678"
    out_canary = "CANARYOUT-ijkl9012"
    prompt_canary = "CANARYPROMPT-mnop3456"
    lines = [
        _assistant(
            {"type": "text", "text": prompt_canary},
            _tool_use("r1", "Read"),
            usage=_usage(1, 0),
        ),
        _user(_tool_result("r1", f"file content {out_canary}" * 100)),
        _assistant(_tool_use("b1", "Bash"), usage=_usage(1, 0)),
        _user(_tool_result("b1", f"echo {cmd_canary}")),
    ]
    _session(tmp_path, "proj", "s1", lines)

    for extra in ([], ["--json"]):
        result = subprocess.run(
            [sys.executable, "-m", "cfxd", "replay", "--root", str(tmp_path), *extra],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        for canary in (path_canary, cmd_canary, out_canary, prompt_canary):
            assert canary not in result.stdout
            assert canary not in result.stderr


def test_cli_json_output(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", _twelve_turn_lines())
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cfxd",
            "replay",
            "--root",
            str(tmp_path),
            "--k",
            "3",
            "--batch",
            "1",
            "--min-bytes",
            "8",
            "--stub-bytes",
            "4",
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["sessions"] == 1
    assert data["baseline_weighted"] == pytest.approx(246.0, abs=0.1)
    assert len(data["policies"]) == 1
    assert data["policies"][0]["k"] == 3
    assert data["policies"][0]["batch"] == 1
    assert len(data["policies"][0]["variants"]) == 6
    assert set(data["cache_expiry"]) == {"<=5m", "5-60m", ">60m"}


def test_cli_human_output(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", [_assistant(usage=_usage(1, 0))])
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "replay", "--root", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "Baseline weighted cost:" in result.stdout
    assert "This report contains counts and numbers only" in result.stdout


def test_cli_empty_root_exits_0(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "replay", "--root", str(empty)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "No transcripts found under empty."


# --- Reset policies R(k, s) ---


def _reset_variant(report: dict, k: int, s: int, variant: str) -> dict:
    for rp in report["reset_policies"]:
        if rp["k"] == k and rp["summary_tokens"] == s:
            for v in rp["variants"]:
                if v["variant"] == variant:
                    return v
    raise AssertionError(f"variant {variant} not found for k={k} S={s}")


def _reset_turn_lines(n: int, out: int = 2) -> list[dict]:
    """n assistant turns, cr = 10*(t+1), no input/creation, output = out."""
    return [_assistant(usage=_usage(0, out, 0, 10 * (t + 1))) for t in range(n)]


def test_reset_below_k_unchanged(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", _reset_turn_lines(4))
    report = replay(tmp_path, "all", reset_ks=(5,), summary_tokens=(100,))

    rp = report["reset_policies"][0]
    assert rp["sessions_affected"] == 0
    assert rp["affected_cost_share"] == 0.0
    ub = _reset_variant(report, 5, 100, "upper_bound")
    assert ub["simulated_weighted"] == pytest.approx(report["baseline_weighted"], abs=0.1)
    assert ub["pct_saved"] == 0.0
    assert ub["pct_saved_affected"] == 0.0


def test_reset_one_reset_known_numbers(tmp_path: Path) -> None:
    lines = [
        _assistant(usage=_usage(100, 2, 0, 0)),  # t0: C=100, weighted 110
        _assistant(usage=_usage(0, 2, 0, 100)),  # t1: C=100, weighted 20
        _assistant(usage=_usage(0, 2, 0, 120)),  # t2: C=120, weighted 22 (reset)
        _assistant(usage=_usage(0, 2, 30, 110)),  # t3: C=140, weighted 58.5
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = replay(tmp_path, "all", reset_ks=(3,), summary_tokens=(50,))

    assert report["baseline_weighted"] == pytest.approx(210.5, abs=0.1)
    rp = report["reset_policies"][0]
    assert rp["sessions_affected"] == 1
    assert rp["affected_cost_share"] == pytest.approx(1.0, abs=1e-9)
    ub = _reset_variant(report, 3, 50, "upper_bound")
    assert ub["simulated_weighted"] == pytest.approx(359.4286, abs=0.1)
    wsc = _reset_variant(report, 3, 50, "with_summary_cost")
    assert wsc["simulated_weighted"] == pytest.approx(621.4286, abs=0.1)


def test_reset_rereset_and_summary_cost(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", _reset_turn_lines(7))
    report = replay(tmp_path, "all", reset_ks=(3,), summary_tokens=(100,))

    assert report["baseline_weighted"] == pytest.approx(98.0, abs=0.1)
    ub = _reset_variant(report, 3, 100, "upper_bound")
    assert ub["simulated_weighted"] == pytest.approx(385.0, abs=0.2)
    wsc = _reset_variant(report, 3, 100, "with_summary_cost")
    assert wsc["simulated_weighted"] == pytest.approx(1394.0, abs=0.2)
    assert wsc["simulated_weighted"] > ub["simulated_weighted"]


def test_reset_compaction_fresh_segment(tmp_path: Path) -> None:
    lines: list[dict] = []
    for t in range(5):
        if t == 3:
            lines.append({"type": "system", "subtype": "compact_boundary"})
        lines.append(_assistant(usage=_usage(0, 1, 0, 10 * (t + 1))))
    _session(tmp_path, "proj", "s1", lines)
    report = replay(tmp_path, "all", reset_ks=(2,), summary_tokens=(0,))

    ub = _reset_variant(report, 2, 0, "upper_bound")
    assert ub["simulated_weighted"] == pytest.approx(94.5, abs=0.2)


def test_reset_cli_json_output(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", _reset_turn_lines(7))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cfxd",
            "replay",
            "--root",
            str(tmp_path),
            "--k",
            "3",
            "--batch",
            "1",
            "--min-bytes",
            "8",
            "--stub-bytes",
            "4",
            "--reset-at",
            "3,5",
            "--summary-tokens",
            "50,100",
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert [(rp["k"], rp["summary_tokens"]) for rp in data["reset_policies"]] == [
        (3, 50),
        (5, 50),
        (3, 100),
        (5, 100),
    ]
    for rp in data["reset_policies"]:
        assert rp["sessions_simulated"] == 1
        assert rp["sessions_affected"] == (1 if 7 >= rp["k"] else 0)
        assert {v["variant"] for v in rp["variants"]} == {"upper_bound", "with_summary_cost"}
    assert [be["summary_tokens"] for be in data["reset_break_even"]] == [50, 100]
