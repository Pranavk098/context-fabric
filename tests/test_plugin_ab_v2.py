"""Tests for the v2 session-length-stratified chain benchmark.

No model is called: ``tests/fake_claude.py`` stands in for ``claude`` and the
oracles are the fixture's own pytest files or the recall scripts.
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

REPO = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).resolve().parent / "fake_claude.py"
FIXTURE_V2 = runner.DEFAULT_FIXTURE.parent / "fixture_v2"
TASKS_V2 = runner.DEFAULT_FIXTURE.parent / "tasks_v2.json"


def _tasks() -> list[runner.Task]:
    return runner.load_tasks(TASKS_V2)


def _run(tasks: list[runner.Task], tmp_path: Path, *, arms: tuple[str, ...], reps: int) -> dict:
    isolation = tmp_path / "iso"
    isolation.mkdir(exist_ok=True)
    return runner.run_experiment(
        tasks,
        arms=arms,
        reps=reps,
        seed=0,
        model="haiku",
        claude_bin=str(FAKE),
        plugin_root=REPO,
        fixture=runner.DEFAULT_FIXTURE,
        prices=prices.load_prices(),
        python=sys.executable,
        fake=True,
        pilot=True,
        run_id="v2",
        report_dir=tmp_path / "reports",
        isolation=isolation,
    )


def test_tasks_v2_shape() -> None:
    tasks = _tasks()
    short = [t for t in tasks if t.stratum == "short"]
    medium = [t for t in tasks if t.stratum == "medium"]
    long = [t for t in tasks if t.stratum == "long"]
    assert len(short) == 4
    assert len(medium) == 4
    assert len(long) == 3
    assert {t.id for t in short} == {t.id for t in runner.load_tasks() if t.kind == "plain"}
    assert all(3 <= len(t.steps) <= 4 for t in medium)
    assert all(8 <= len(t.steps) <= 12 for t in long)
    assert all(t.steps[-1].oracle.startswith("oracles/") for t in long)
    for task in tasks:
        for step in task.steps:
            assert step.id and step.prompt and step.oracle
            assert step.timeout > 0


@pytest.mark.parametrize("task", [t for t in _tasks() if not t.steps], ids=lambda t: t.id)
def test_short_v2_oracles_fail_then_pass(tmp_path: Path, task: runner.Task) -> None:
    work = tmp_path / task.id
    shutil.copytree(
        runner.DEFAULT_FIXTURE, work, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache")
    )
    apply_patch(work, task.setup_patch)
    assert runner.run_oracle(sys.executable, work, task.oracle) is False
    apply_patch(work, task.reference_patch)
    assert runner.run_oracle(sys.executable, work, task.oracle) is True


@pytest.mark.parametrize("task", [t for t in _tasks() if t.steps], ids=lambda t: t.id)
def test_chain_steps_fail_then_pass(tmp_path: Path, task: runner.Task) -> None:
    work = tmp_path / task.id
    shutil.copytree(FIXTURE_V2, work, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    for step in task.steps:
        before = runner.run_step_oracle(sys.executable, work, step.oracle, 120)
        assert before is not None, f"{task.id}/{step.id} oracle could not run"
        assert before[0] is False, f"{task.id}/{step.id} oracle passed before the patch"
        apply_patch(work, step.reference_patch)
        if step.fake_answers is not None:
            (work / "answers.json").write_text(json.dumps(step.fake_answers), encoding="utf-8")
        after = runner.run_step_oracle(sys.executable, work, step.oracle, 120)
        assert after is not None, f"{task.id}/{step.id} oracle could not run after the patch"
        assert after[0] is True, f"{task.id}/{step.id} oracle failed after the patch"


def test_fixture_v2_reaches_a_green_state(tmp_path: Path) -> None:
    work = tmp_path / "fixture_v2"
    shutil.copytree(FIXTURE_V2, work, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    seen: set[str] = set()
    for task in _tasks():
        for step in task.steps:
            if step.id in seen:
                continue
            seen.add(step.id)
            apply_patch(work, step.reference_patch)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=work,
        capture_output=True,
        timeout=300,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert proc.returncode == 0, proc.stdout.decode("utf-8", "replace")[-2000:]


def test_chain_execution_uses_session_then_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[list[str]] = []
    original = runner.build_command

    def spy(*args: object, **kwargs: object) -> list[str]:
        cmd = original(*args, **kwargs)  # type: ignore[arg-type]
        commands.append(cmd)
        return cmd

    monkeypatch.setattr(runner, "build_command", spy)
    task = next(t for t in _tasks() if t.id == "medium-refunds")
    report = _run([task], tmp_path, arms=("off",), reps=0)
    record = report["runs"][0]
    assert [s["id"] for s in record["steps"]] == ["money", "refund-amount", "refund-cap"]
    assert record["resolved"] is True
    assert len(commands) == 3
    assert commands[0][commands[0].index("--session-id") + 1] == record["session_id"]
    for cmd in commands[1:]:
        assert cmd[cmd.index("--resume") + 1] == record["session_id"]
        assert "--session-id" not in cmd
    assert all(s["turns"] > 0 for s in record["steps"])
    assert all(s["dollars"] is not None for s in record["steps"])


def test_recall_probe_is_scored_in_the_chain(tmp_path: Path) -> None:
    tasks = _tasks()
    money = next(t for t in tasks if t.id == "medium-tax").steps[0]
    recall = next(t for t in tasks if t.id == "long-checkout").steps[-1]
    task = runner.Task(
        "chain-recall",
        "chain",
        "",
        {},
        "",
        {},
        [],
        stratum="long",
        steps=(money, recall),
        fixture="fixture_v2",
    )
    report = _run([task], tmp_path, arms=("off",), reps=0)
    record = report["runs"][0]
    assert record["resolved"] is True
    assert record["recall_score"] == 3
    assert record["recall_of"] == 3
    assert record["steps"][-1]["recall_score"] == 3


def test_cap_hit_stops_the_chain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EVALS_MAX_RUN_USD", "0.000001")
    task = next(t for t in _tasks() if t.id == "medium-refunds")
    report = _run([task], tmp_path, arms=("off",), reps=0)
    record = report["runs"][0]
    assert record["cap_hit"] is True
    assert record["resolved"] is False
    assert len(record["steps"]) < len(task.steps)


def test_chain_infra_failure_is_recorded_and_excluded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PLUGIN_AB_FAKE_ERROR", "1")
    task = next(t for t in _tasks() if t.id == "medium-refunds")
    report = _run([task], tmp_path, arms=("off",), reps=0)
    record = report["runs"][0]
    assert record["infra_failure"] is True
    assert "Not logged in" in record["infra_reason"]
    result = analyze.analyze_v2([record])
    assert result["infra_excluded"] == 1


def _rec(
    task: str,
    stratum: str,
    arm: str,
    dollars: float,
    *,
    recall: int | None = None,
    checkpoint: bool = False,
) -> dict[str, object]:
    steps: list[dict[str, object]] = [
        {"id": "work", "resolved": True, "turns": 4, "dollars": dollars / 2},
    ]
    if checkpoint:
        steps.append({"id": "checkpoint", "resolved": True, "turns": 5, "dollars": 0.0})
        steps.append({"id": "after", "resolved": True, "turns": 7, "dollars": dollars / 2})
    return {
        "task": task,
        "stratum": stratum,
        "kind": "chain",
        "arm": arm,
        "rep": 0,
        "warm_up": False,
        "resolved": True,
        "cap_hit": False,
        "dollars": dollars,
        "steps": steps,
        "recall_score": recall,
        "recall_of": 3 if recall is not None else None,
    }


def test_analyzer_v2_exact_ratios_and_claim_columns() -> None:
    records: list[dict[str, object]] = []
    plan = {
        "short": ("s", {"off": 100.0, "shipped": 105.0, "checkpoint": 103.0}),
        "medium": ("m", {"off": 100.0, "shipped": 85.0, "checkpoint": 83.0}),
        "long": ("l", {"off": 100.0, "shipped": 80.0, "checkpoint": 78.0}),
    }
    for stratum, (task, dollars) in plan.items():
        for arm, value in dollars.items():
            records.append(_rec(task, stratum, arm, value, recall=3, checkpoint=stratum == "long"))
    result = analyze.analyze_v2(records)
    short = result["strata"]["short"]["arms"]["shipped"]
    medium = result["strata"]["medium"]["arms"]["shipped"]
    long = result["strata"]["long"]["arms"]["shipped"]
    assert short["geometric_mean_ratio"] == pytest.approx(1.05)
    assert short["ratio_pass"] is True
    assert short["ci_pass"] is None
    assert short["claim_met"] is True
    assert medium["geometric_mean_ratio"] == pytest.approx(0.85)
    assert medium["bootstrap_95ci"] == pytest.approx([0.85, 0.85])
    assert medium["holm_pass"] is True
    assert medium["claim_met"] is True
    assert long["geometric_mean_ratio"] == pytest.approx(0.80)
    assert long["bootstrap_95ci"] == pytest.approx([0.80, 0.80])
    assert long["step_pass_rate"] == pytest.approx(1.0)
    assert long["recall"]["mean_score"] == pytest.approx(3.0)
    assert long["extra_turns_after_checkpoint"] == pytest.approx(7.0)
    assert long["success_pass"] is True
    assert long["recall_pass"] is True
    assert long["claim_met"] is True
    assert set(result["strata"]["long"]["arms"]) == {"shipped", "checkpoint"}
    assert result["arms"] == ["off", "shipped", "checkpoint"]
    markdown = analyze.format_markdown_v2(result)
    assert "## long (1 tasks)" in markdown
    assert "diet" not in markdown and "combo" not in markdown
    assert "claim" in markdown


def test_analyzer_v2_excludes_slice0_records() -> None:
    record = _rec("l", "long", "off", 100.0, checkpoint=True)
    record["slice"] = "slice0"
    result = analyze.analyze_v2([record, {**record, "arm": "checkpoint"}])
    assert result["slice0_excluded"] == 2
    assert result["strata"] == {}


def test_v2_prereg_gate_refuses_without_hash_or_on_mismatch(tmp_path: Path) -> None:
    (tmp_path / "PROTOCOL_V2.md").write_text("p", encoding="utf-8")
    (tmp_path / "tasks_v2.json").write_text("t", encoding="utf-8")
    (tmp_path / "prices.json").write_text("$", encoding="utf-8")
    ok, message = prereg.gate_ok_v2(tmp_path, pilot=False)
    assert ok is False and "missing prereg_v2.sha256" in message
    prereg.write_hash_v2(tmp_path)
    assert prereg.gate_ok_v2(tmp_path, pilot=False)[0] is True
    (tmp_path / "PROTOCOL_V2.md").write_text("changed", encoding="utf-8")
    ok, message = prereg.gate_ok_v2(tmp_path, pilot=False)
    assert ok is False and "does not match" in message
    assert prereg.gate_ok_v2(tmp_path, pilot=True)[0] is True
    # The v1 gate and hash file are untouched.
    assert prereg.gate_ok(tmp_path, pilot=False) == (False, "missing prereg.sha256")


def test_v2_runner_refuses_a_non_pilot_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runner.prereg.gate_ok_v2 = lambda directory, pilot: (False, "missing prereg_v2.sha256")  # type: ignore[assignment]
    assert runner.main(["--tasks", str(TASKS_V2), "--stratum", "medium"]) == 2
    assert "missing prereg_v2.sha256" in capsys.readouterr().err


def test_v2_runner_accepts_a_matching_hash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(tasks: list[runner.Task], **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(runner.prereg, "gate_ok_v2", lambda directory, pilot: (True, "ok"))
    monkeypatch.setattr(runner, "run_experiment", fake_run)
    code = runner.main(
        ["--tasks", str(TASKS_V2), "--stratum", "medium", "--report-dir", str(tmp_path)]
    )
    assert code == 0
    assert captured["arms"] == runner.V2_ARMS


def test_v2_rejects_diet_and_combo(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(runner.prereg, "gate_ok_v2", lambda directory, pilot: (True, "ok"))
    for arm in ("diet", "combo"):
        assert runner.main(["--tasks", str(TASKS_V2), "--arms", arm]) == 2
        assert "registered arms are off,shipped,checkpoint" in capsys.readouterr().err


def test_checkpoint_arm_saves_and_restarts_the_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[list[str]] = []
    fresh_envs: list[list[str]] = []
    original = runner.build_command

    def spy_build(*args: object, **kwargs: object) -> list[str]:
        cmd = original(*args, **kwargs)  # type: ignore[arg-type]
        commands.append(cmd)
        return cmd

    original_run = runner.subprocess.run

    def spy_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        fresh_envs.append(cmd)
        return original_run(cmd, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "build_command", spy_build)
    monkeypatch.setattr(runner.subprocess, "run", spy_run)
    monkeypatch.setattr(runner, "save_checkpoint", lambda python, work, cfx_home, port: 321)
    monkeypatch.setattr(
        runner, "checkpoint_block", lambda python, work, cfx_home, port: "BLOCK-FROM-CHECKPOINT"
    )

    task = next(t for t in _tasks() if t.id == "long-checkout")
    report = _run([task], tmp_path, arms=("checkpoint",), reps=0)
    record = report["runs"][0]
    assert record["boundaries_taken"] == [4, 8]
    assert record["checkpoint_saved_bytes"] == 642
    assert record["checkpoint_restore_outcome"] == "injected"
    assert len(record["checkpoints"]) == 2
    assert all(c["restore_outcome"] == "injected" for c in record["checkpoints"])
    # The restored block is prefixed to the first prompt of each new session only.
    prompts = [cmd[cmd.index("-p") + 1] for cmd in commands]
    assert [i for i, text in enumerate(prompts) if text.startswith("BLOCK-FROM-CHECKPOINT")] == [
        4,
        8,
    ]

    sessions = [
        cmd[cmd.index("--session-id") + 1]
        if "--session-id" in cmd
        else cmd[cmd.index("--resume") + 1]
        for cmd in commands
    ]
    assert sessions[0] == sessions[1] == sessions[2] == sessions[3]
    assert sessions[4] != sessions[0]
    assert "--resume" not in commands[4]
    assert sessions[5] == sessions[6] == sessions[7] == sessions[4]
    assert sessions[8] != sessions[4]
    assert "--resume" not in commands[8]
    assert sessions[9] == sessions[10] == sessions[11] == sessions[8]


def test_off_and_shipped_do_not_take_checkpoints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[list[str]] = []
    original = runner.build_command

    def spy_build(*args: object, **kwargs: object) -> list[str]:
        cmd = original(*args, **kwargs)  # type: ignore[arg-type]
        commands.append(cmd)
        return cmd

    monkeypatch.setattr(runner, "build_command", spy_build)
    monkeypatch.setattr(
        runner,
        "save_checkpoint",
        lambda *args: pytest.fail("off/shipped must not save checkpoints"),
    )
    task = next(t for t in _tasks() if t.id == "medium-refunds")
    report = _run([task], tmp_path, arms=("off", "shipped"), reps=0)
    assert all("checkpoints" not in record for record in report["runs"])
    assert all("--resume" in cmd for cmd in commands[1::3])  # steps 2-3 of each run
    assert len(commands) == 6


def test_slice0_plan_is_five_runs_in_order() -> None:
    specs = runner.plan_slice0(_tasks())
    assert len(specs) == 5
    assert specs[0].task_id == "long-checkout"
    assert specs[0].warm_up is True and specs[0].arm == "off"
    assert [(s.arm, s.rep) for s in specs[1:]] == [
        ("off", 0),
        ("checkpoint", 0),
        ("checkpoint", 1),
        ("off", 1),
    ]
    assert not any(s.warm_up for s in specs[1:])


def test_slice0_cli_pins_model_and_labels_the_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_run(tasks: list[runner.Task], **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(runner.prereg, "gate_ok_v2", lambda directory, pilot: (True, "ok"))
    monkeypatch.setattr(runner, "run_experiment", fake_run)
    code = runner.main(["--tasks", str(TASKS_V2), "--slice0", "--report-dir", str(tmp_path)])
    assert code == 0
    assert captured["slice_name"] == "slice0"
    assert captured["arms"] == ("off", "checkpoint")
    assert captured["model"] == "claude-haiku-5-5"


def test_stratum_filters_tasks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tasks = _tasks()
    seen: list[str] = []

    def fake_run_one(
        task: runner.Task, spec: runner.RunSpec, **kwargs: object
    ) -> dict[str, object]:
        seen.append(task.id)
        return {"task": task.id}

    monkeypatch.setattr(runner, "run_one", fake_run_one)
    runner.run_experiment(
        tasks,
        arms=("off", "shipped"),
        reps=1,
        seed=0,
        model="haiku",
        claude_bin="claude",
        plugin_root=REPO,
        fixture=runner.DEFAULT_FIXTURE,
        prices={},
        python=sys.executable,
        fake=False,
        pilot=True,
        run_id="filter",
        report_dir=tmp_path / "reports",
        isolation=tmp_path / "iso",
        stratum="medium",
    )
    assert set(seen) == {t.id for t in tasks if t.stratum == "medium"}
