"""Tests for the paired plugin A/B benchmark harness.

Every test runs with zero model cost: the "oracle" is the fixture's own pytest
tests, and the "claude" process is ``tests/fake_claude.py``, a deterministic
stand-in that writes transcripts and (in the solving step) applies the task's
reference patch.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from evals.plugin_ab import analyze, prereg, prices, runner
from evals.plugin_ab.patch import apply_patch
from evals.plugin_ab.usage import ScanResult

REPO = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).resolve().parent / "fake_claude.py"


def _copy_fixture(dest: Path) -> Path:
    shutil.copytree(
        runner.DEFAULT_FIXTURE, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache")
    )
    return dest


def _run_oracle(work: Path, oracle: str) -> bool:
    runner.purge_pycache(work)
    env = dict(**os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", oracle],
        cwd=work,
        capture_output=True,
        timeout=300,
        env=env,
    )
    return proc.returncode == 0


def test_fixture_tests_pass(tmp_path: Path) -> None:
    dest = _copy_fixture(tmp_path / "fixture")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=dest,
        capture_output=True,
        timeout=300,
        env=dict(**os.environ, PYTHONDONTWRITEBYTECODE="1"),
    )
    assert proc.returncode == 0


@pytest.mark.parametrize("task", runner.load_tasks(), ids=lambda t: t.id)
def test_oracle_fails_before_passes_after(tmp_path: Path, task: runner.Task) -> None:
    broken = tmp_path / task.id
    _copy_fixture(broken)
    apply_patch(broken, task.setup_patch)
    assert not _run_oracle(broken, task.oracle), f"{task.id} oracle passed before the fix"
    apply_patch(broken, task.reference_patch)
    assert _run_oracle(broken, task.oracle), f"{task.id} oracle failed after the reference patch"


def test_plan_runs_balanced_and_seeded() -> None:
    tasks = runner.load_tasks()
    arms = ("off", "on")
    assert runner.plan_runs(tasks, arms, 3, 0) == runner.plan_runs(tasks, arms, 3, 0)
    specs = runner.plan_runs(tasks, arms, 3, 7)
    assert len(specs) == len(tasks) * len(arms) * 4  # 1 warm-up + 3 measured per cell
    assert all(spec.warm_up == (spec.rep == -1) for spec in specs)
    for task in tasks:
        measured = [s for s in specs if s.task_id == task.id and not s.warm_up]
        seq = [s.arm for s in sorted(measured, key=lambda s: s.order_index)]
        assert len([a for a in seq if a == "off"]) == 3
        assert len([a for a in seq if a == "on"]) == 3
        # ABBA: x, y, y, x, x, y
        assert seq == [seq[0], seq[1], seq[1], seq[0], seq[0], seq[1]]


def test_isolation_distinct_cfx_and_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: list[dict[str, str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        env = kwargs.get("env")
        if isinstance(env, dict):
            captured.append({k: v for k, v in env.items() if k in ("CFX_HOME", "CFX_PORT")})
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner, "_copy_fixture", lambda fixture, dest: None)
    monkeypatch.setattr(runner, "apply_patch", lambda root, patch: None)
    monkeypatch.setattr(runner, "run_oracle", lambda python, work, oracle: True)
    monkeypatch.setattr(
        runner,
        "collect_session_usage",
        lambda config_dir, sid, prices: (ScanResult(), ScanResult(), ScanResult(), 0),
    )
    task = runner.load_tasks()[0]
    for rep in (-1, 0):
        runner.run_one(
            task,
            runner.RunSpec(task.id, "off", rep, rep == -1, rep),
            model="haiku",
            claude_bin="claude",
            plugin_root=REPO,
            fixture=runner.DEFAULT_FIXTURE,
            isolation=tmp_path,
            prices={},
            python=sys.executable,
            fake=False,
        )
    homes = {c["CFX_HOME"] for c in captured}
    ports = {c["CFX_PORT"] for c in captured}
    assert len(homes) == 2
    assert len(ports) == 2


def _write_jsonl(path: Path, objs: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for obj in objs:
            fh.write(json.dumps(obj) + "\n")


def test_collect_usage_includes_subagents(tmp_path: Path) -> None:
    config = tmp_path / "config"
    sess_dir = config / "projects" / "slug" / "sess"
    _write_jsonl(
        sess_dir / "sess.jsonl",
        [
            {
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                    "model": "claude-haiku-4-5-20251001",
                },
            }
        ],
    )
    _write_jsonl(
        sess_dir / "sess" / "subagents" / "sess-sub.jsonl",
        [
            {
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 200,
                        "output_tokens": 20,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                    "model": "claude-haiku-4-5-20251001",
                },
            }
        ],
    )
    parent, subs, total, count = runner.collect_session_usage(config, "sess", prices.load_prices())
    assert count == 1
    assert subs.input_tokens == 200
    assert total.input_tokens == 300
    assert total.output_tokens == 30
    assert parent.input_tokens == 100


def test_weighted_tokens_and_dollars(tmp_path: Path) -> None:
    table = prices.load_prices()
    transcript = tmp_path / "t.jsonl"

    def scan_line(usage: dict[str, int]) -> ScanResult:
        _write_jsonl(
            transcript,
            [
                {
                    "type": "assistant",
                    "message": {"usage": usage, "model": "claude-haiku-4-5-20251001"},
                }
            ],
        )
        from evals.plugin_ab.usage import scan_transcript

        return scan_transcript(transcript, table)

    merged = scan_line(
        {
            "input_tokens": 1000,
            "cache_creation_input_tokens": 2000,
            "cache_read_input_tokens": 500,
            "output_tokens": 100,
        }
    )
    assert merged.weighted() == pytest.approx(1000 + 1.25 * 2000 + 0.1 * 500 + 5 * 100)
    assert merged.dollars == pytest.approx((1000 * 1 + 2000 * 1.25 + 500 * 0.10 + 100 * 5) / 1e6)

    by_ttl = scan_line(
        {
            "input_tokens": 1000,
            "cache_creation_input_tokens_5m": 2000,
            "cache_creation_input_tokens_1h": 1000,
            "cache_read_input_tokens": 500,
            "output_tokens": 100,
        }
    )
    assert by_ttl.cache_creation_input_tokens == 3000
    assert by_ttl.dollars == pytest.approx(
        (1000 * 1 + 2000 * 1.25 + 1000 * 2.0 + 500 * 0.10 + 100 * 5) / 1e6
    )


def test_unpriced_model(tmp_path: Path) -> None:
    table = prices.load_prices()
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "assistant",
                "message": {
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                    "model": "claude-3-5-haiku-20241022",
                },
            }
        ],
    )
    from evals.plugin_ab.usage import scan_transcript

    result = scan_transcript(transcript, table)
    assert result.dollars is None
    assert "claude-3-5-haiku-20241022" in result.unpriced_models


def test_detect_brief_and_carry(tmp_path: Path) -> None:
    cfx = tmp_path / "cfx"
    cfx.mkdir()
    assert not runner.detect_brief(cfx)
    assert not runner.detect_carry(cfx)
    _write_jsonl(cfx / "briefs.jsonl", [{"decision": "briefed"}])
    _write_jsonl(cfx / "carry.jsonl", [{"decision": "rendered"}])
    assert runner.detect_brief(cfx)
    assert runner.detect_carry(cfx)


def _rec(
    task: str,
    arm: str,
    dollars: float,
    *,
    warm_up: bool = False,
    rep: int = 0,
    resolved: bool = True,
    kind: str = "plain",
    brief: bool | None = None,
    carry: bool | None = None,
) -> dict[str, object]:
    r: dict[str, object] = {
        "task": task,
        "kind": kind,
        "arm": arm,
        "rep": rep,
        "warm_up": warm_up,
        "resolved": resolved,
        "dollars": dollars,
    }
    if brief is not None:
        r["brief_fired"] = brief
    if carry is not None:
        r["carry_fired"] = carry
    return r


def test_warmup_excluded() -> None:
    records = [
        _rec("t", "off", 1e9, warm_up=True),
        _rec("t", "on", 1e9, warm_up=True),
        _rec("t", "off", 100.0),
        _rec("t", "on", 100.0),
    ]
    result = analyze.analyze(records)
    assert result["per_task"]["t"]["on_off_ratio"] == pytest.approx(1.0)


def test_analyze_arithmetic_and_bootstrap_deterministic() -> None:
    ratios = {"t1": 0.5, "t2": 0.8, "t3": 2.0, "t4": 1.0}
    records: list[dict[str, object]] = []
    for task, ratio in ratios.items():
        for rep in range(2):
            records.append(_rec(task, "off", 100.0, rep=rep))
            records.append(_rec(task, "on", round(100.0 * ratio, 4), rep=rep))
    result = analyze.analyze(records)
    assert result["geometric_mean_ratio"] == pytest.approx(round(0.8**0.25, 4))
    assert result["losses"] == ["t3"]
    assert result["wins"] == ["t1", "t2"]
    assert result["bootstrap_95ci"] == analyze.analyze(records)["bootstrap_95ci"]
    assert result["non_inferiority_met"] is True


def test_per_protocol_vs_intention_to_treat() -> None:
    records = [
        _rec("s", "off", 100.0),
        _rec("s", "on", 40.0, kind="subagent", brief=True),
        _rec("s", "on", 100.0, kind="subagent", brief=False),
        _rec("s", "on", 40.0, kind="subagent", brief=True),
    ]
    entry = analyze.analyze(records)["per_task"]["s"]
    assert entry["on_mean_dollars"] == pytest.approx(60.0)  # intention-to-treat
    assert entry["on_mean_dollars_per_protocol"] == pytest.approx(40.0)  # brief fired only


def test_prereg_gate(tmp_path: Path) -> None:
    (tmp_path / "PROTOCOL.md").write_text("p", encoding="utf-8")
    (tmp_path / "tasks.json").write_text("t", encoding="utf-8")
    (tmp_path / "prices.json").write_text("$", encoding="utf-8")
    prereg.write_hash(tmp_path)
    assert prereg.gate_ok(tmp_path, pilot=False)[0] is True
    (tmp_path / "PROTOCOL.md").write_text("changed", encoding="utf-8")
    ok, message = prereg.gate_ok(tmp_path, pilot=False)
    assert ok is False and "does not match" in message
    assert prereg.gate_ok(tmp_path, pilot=True)[0] is True
    empty = tmp_path / "empty"
    empty.mkdir()
    ok, message = prereg.gate_ok(empty, pilot=False)
    assert ok is False and "missing" in message


def test_pilot_capped_at_eight_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[runner.RunSpec] = []

    def fake_run_one(
        task: runner.Task, spec: runner.RunSpec, **kwargs: object
    ) -> dict[str, object]:
        calls.append(spec)
        return {"task": spec.task_id}

    monkeypatch.setattr(runner, "run_one", fake_run_one)
    report = runner.run_experiment(
        runner.load_tasks(),
        arms=("off", "on"),
        reps=3,
        seed=0,
        model="haiku",
        claude_bin="claude",
        plugin_root=REPO,
        fixture=runner.DEFAULT_FIXTURE,
        prices={},
        python=sys.executable,
        fake=False,
        pilot=True,
        run_id="pilot",
        report_dir=tmp_path / "reports",
        isolation=tmp_path / "iso",
        max_runs=8,
    )
    assert len(calls) == 8
    assert len(report["runs"]) == 8
    assert report["mode"] == "pilot"


def test_end_to_end_fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PLUGIN_AB_CLAUDE", str(FAKE))
    tasks = runner.load_tasks()
    selected = [
        next(t for t in tasks if t.kind == kind) for kind in ("plain", "subagent", "compact")
    ]
    report_dir = tmp_path / "reports"
    isolation = tmp_path / "iso"
    isolation.mkdir()
    report = runner.run_experiment(
        selected,
        arms=("off", "on"),
        reps=1,
        seed=1,
        model="haiku",
        claude_bin=str(FAKE),
        plugin_root=REPO,
        fixture=runner.DEFAULT_FIXTURE,
        prices=prices.load_prices(),
        python=sys.executable,
        fake=True,
        pilot=True,
        run_id="e2e",
        report_dir=report_dir,
        isolation=isolation,
    )
    records = report["runs"]
    assert len(records) == 3 * 2 * 2  # 3 tasks * 2 arms * (warm-up + 1 measured)
    assert all(r["resolved"] for r in records)
    subagent_on = [r for r in records if r["kind"] == "subagent" and r["arm"] == "on"][0]
    assert subagent_on["usage"]["subagent_sessions"] == 1
    text = (report_dir / "e2e.json").read_text(encoding="utf-8")
    for task in selected:
        assert task.prompt not in text
        assert json.dumps(task.reference_patch, sort_keys=True) not in text


def test_turn_tokens_reads_the_nested_cache_creation_split() -> None:
    # Real transcripts carry usage.cache_creation.{ephemeral_5m,ephemeral_1h}_input_tokens; the
    # 1-hour write costs 2x base input versus 1.25x, so merging them understates dollars.
    from evals.plugin_ab.usage import _turn_tokens

    usage = {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_input_tokens": 30,
        "cache_creation_input_tokens": 300,
        "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200},
    }
    assert _turn_tokens(usage) == (10, 100, 200, 30, 20)
    # no split available: the merged field is priced at the 5-minute rate (documented)
    assert _turn_tokens({"cache_creation_input_tokens": 50}) == (0, 50, 0, 0, 0)


def test_model_ids_map_to_their_own_price_rows() -> None:
    from evals.plugin_ab.prices import normalize_model

    assert normalize_model("claude-opus-5-5") == "opus-5.5"
    assert normalize_model("claude-opus-5") == "opus-5"
    assert normalize_model("claude-opus-4-8") == "opus-4.8"
    assert normalize_model("claude-sonnet-5") == "sonnet-5"
    assert normalize_model("claude-sonnet-5-5") == "sonnet-5.5"
    assert normalize_model("claude-haiku-4-5-20251001") == "haiku-4.5"
    assert normalize_model("claude-haiku-5-5") == "haiku-5.5"
    assert normalize_model("claude-fable-5-1") == "fable-5.1"
    assert normalize_model("<synthetic>") is None


def test_real_runs_keep_the_login_and_skip_user_settings(tmp_path: Path) -> None:
    # A fresh CLAUDE_CONFIG_DIR means "Not logged in", so real runs use the real config home and
    # isolate with --setting-sources instead (user-scope settings and plugins are not loaded).
    from evals.plugin_ab.runner import _claude_home, build_command

    run_dir = tmp_path / "run"
    cfg, override = _claude_home(fake=False, run_dir=run_dir)
    assert override is False
    assert cfg != run_dir / "claude"
    cfg, override = _claude_home(fake=True, run_dir=run_dir)
    assert override is True
    assert cfg == run_dir / "claude"

    cmd = build_command(
        "claude",
        prompt="p",
        model="haiku",
        session_id="s",
        arm="off",
        plugin_root=tmp_path,
    )
    i = cmd.index("--setting-sources")
    assert cmd[i + 1] == "project,local"
    assert "--plugin-dir" not in cmd


def test_session_ids_are_valid_uuids() -> None:
    # Claude Code rejects anything else ("Invalid session ID. Must be a valid UUID").
    import uuid

    ids = {runner._new_session_id() for _ in range(5)}
    assert len(ids) == 5
    for sid in ids:
        assert str(uuid.UUID(sid)) == sid


def test_infra_failure_is_recorded_and_two_in_a_row_stop_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PLUGIN_AB_CLAUDE", str(FAKE))
    monkeypatch.setenv("PLUGIN_AB_FAKE_ERROR", "1")
    plain = next(t for t in runner.load_tasks() if t.kind == "plain")
    report = runner.run_experiment(
        [plain],
        arms=("off", "on"),
        reps=2,
        seed=0,
        model="haiku",
        claude_bin=str(FAKE),
        plugin_root=REPO,
        fixture=runner.DEFAULT_FIXTURE,
        prices=prices.load_prices(),
        python=sys.executable,
        fake=True,
        pilot=True,
        run_id="infra",
        report_dir=tmp_path / "reports",
        isolation=tmp_path / "iso",
    )
    assert len(report["runs"]) == 2  # stopped after two consecutive infrastructure failures
    assert all(r["infra_failure"] for r in report["runs"])
    assert "Not logged in" in report["runs"][0]["infra_reason"]
    assert "infrastructure" in report["aborted"]


def test_analyzer_excludes_infrastructure_failures() -> None:
    def rec(task: str, arm: str, dollars: float | None, **extra: object) -> dict[str, object]:
        base: dict[str, object] = {
            "task": task,
            "kind": "plain",
            "arm": arm,
            "rep": 1,
            "warm_up": False,
            "resolved": True,
            "dollars": dollars,
            "weighted_tokens": 1.0,
            "turns": 1,
        }
        base.update(extra)
        return base

    records = [
        rec("t1", "off", 1.0),
        rec("t1", "on", 0.5),
        {
            "task": "t1",
            "kind": "plain",
            "arm": "on",
            "rep": 2,
            "warm_up": False,
            "infra_failure": True,
        },
    ]
    result = analyze.analyze(records)
    assert result["infra_excluded"] == 1
    assert result["geometric_mean_ratio"] == pytest.approx(0.5)
