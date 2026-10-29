"""Tests for the v3 exploratory adversarial benchmark (fake claude only, no model call)."""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from evals.plugin_ab import analyze, prereg, prices, runner
from evals.plugin_ab.patch import apply_patch

REPO = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).resolve().parent / "fake_claude.py"
TASKS_V3 = runner.DEFAULT_FIXTURE.parent / "tasks_v3_adversarial.json"
TASKS_V2 = runner.DEFAULT_FIXTURE.parent / "tasks_v2.json"
FIXTURE_V3 = runner.DEFAULT_FIXTURE.parent / "fixture_v3"
IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache")


def _tasks() -> list[runner.Task]:
    return runner.load_tasks(TASKS_V3)


def _run(tasks: list[runner.Task], tmp_path: Path, *, arms: tuple[str, ...], smoke: bool) -> dict:
    isolation = tmp_path / "iso"
    isolation.mkdir(exist_ok=True)
    return runner.run_experiment(
        tasks,
        arms=arms,
        reps=1,
        seed=0,
        model="haiku",
        claude_bin=str(FAKE),
        plugin_root=REPO,
        fixture=runner.DEFAULT_FIXTURE,
        prices=prices.load_prices(),
        python=sys.executable,
        fake=True,
        pilot=False,
        run_id="v3",
        report_dir=tmp_path / "reports",
        isolation=isolation,
        exploratory=True,
        smoke=smoke,
    )


def test_tasks_v3_shape() -> None:
    tasks = _tasks()
    assert [t.id for t in tasks] == [
        "control-a",
        "control-b",
        "adv-killed-test",
        "adv-outside-edit",
        "adv-revert",
        "adv-rename",
    ]
    assert len(tasks) == 6
    assert all(t.fixture == "fixture_v3" and t.exploratory for t in tasks)
    assert all(len(t.steps) == 6 for t in tasks)
    controls = [t for t in tasks if t.id.startswith("control")]
    adversarial = [t for t in tasks if t.id.startswith("adv")]
    assert len(controls) == 2 and len(adversarial) == 4
    assert not any(s.between or s.killed for t in controls for s in t.steps)
    assert all(any(s.between or s.killed for s in t.steps) for t in adversarial)
    assert sum(1 for t in adversarial if any(s.killed for s in t.steps)) == 1
    assert all(t.steps[-1].claim_check for t in adversarial)
    for task in tasks:
        for step in task.steps:
            assert step.id and step.prompt and step.oracle and step.timeout > 0


def test_v2_tasks_carry_no_v3_features() -> None:
    v2 = runner.load_tasks(TASKS_V2)
    assert not runner.is_v3(v2)
    assert not any(step.between or step.killed for task in v2 for step in task.steps)


def test_between_actions_applied_in_order(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("first", encoding="utf-8")
    snapshots = [runner.snapshot_tree(work)]

    runner.apply_between(
        work,
        [
            {"op": "write", "path": "a.txt", "content": "second"},
            {"op": "write", "path": "a.txt", "content": "third"},
        ],
        snapshots,
    )
    assert (work / "a.txt").read_text(encoding="utf-8") == "third"

    snapshots.append(runner.snapshot_tree(work))
    runner.apply_between(
        work,
        [
            {"op": "write", "path": "a.txt", "content": "fourth"},
            {"op": "revert", "path": "a.txt"},
        ],
        snapshots,
    )
    assert (work / "a.txt").read_text(encoding="utf-8") == "third"

    runner.apply_between(work, [{"op": "rename", "from": "a.txt", "to": "sub/b.txt"}], snapshots)
    assert (work / "sub" / "b.txt").read_text(encoding="utf-8") == "third"
    assert not (work / "a.txt").exists()

    with pytest.raises(ValueError, match="inside the workspace"):
        runner.apply_between(work, [{"op": "write", "path": "../x", "content": ""}], snapshots)
    with pytest.raises(ValueError, match="unknown between op"):
        runner.apply_between(work, [{"op": "chmod"}], snapshots)


def test_killed_step_is_recorded_as_killed(tmp_path: Path) -> None:
    task = next(t for t in _tasks() if t.id == "adv-killed-test")
    report = _run([task], tmp_path, arms=("off",), smoke=False)
    record = report["runs"][0]
    outcomes = {step["id"]: step["outcome"] for step in record["steps"]}
    assert outcomes["killed-report-build"] == "killed"
    assert outcomes["claim"] == "pass"
    assert record["resolved"] is False
    killed = next(step for step in record["steps"] if step["outcome"] == "killed")
    assert killed["resolved"] is False
    assert sum(1 for step in record["steps"] if step.get("misleading_claim")) == 0


def test_killed_step_oracle_would_pass_when_not_killed(tmp_path: Path) -> None:
    task = next(t for t in _tasks() if t.id == "adv-killed-test")
    step = next(s for s in task.steps if s.killed)
    work = tmp_path / "work"
    shutil.copytree(FIXTURE_V3, work, ignore=IGNORE)
    assert runner.run_step_oracle(sys.executable, work, step.oracle, 120)[0] is False
    apply_patch(work, step.reference_patch)
    assert runner.run_step_oracle(sys.executable, work, step.oracle, 120)[0] is True


def test_v3_refused_without_exploratory(capsys: pytest.CaptureFixture[str]) -> None:
    assert runner.main(["--tasks", str(TASKS_V3)]) == 2
    assert "--exploratory" in capsys.readouterr().err


def test_exploratory_flag_needs_v3_tasks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(runner.prereg, "gate_ok_v2", lambda directory, pilot: (True, "ok"))
    assert runner.main(["--tasks", str(TASKS_V2), "--exploratory"]) == 2
    assert "needs the v3" in capsys.readouterr().err


def test_smoke_needs_the_fake_claude(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PLUGIN_AB_CLAUDE", raising=False)
    assert runner.main(["--tasks", str(TASKS_V3), "--exploratory", "--smoke"]) == 2
    assert "fake claude only" in capsys.readouterr().err


def test_smoke_plan_is_one_chain_two_arms() -> None:
    specs = runner.plan_smoke(_tasks())
    assert [(spec.task_id, spec.arm) for spec in specs] == [
        ("adv-killed-test", "off"),
        ("adv-killed-test", "checkpoint"),
    ]
    assert not any(spec.warm_up for spec in specs)


def test_v3_prereg_gate_is_not_in_play(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(tasks: list[runner.Task], **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(runner, "run_experiment", fake_run)
    monkeypatch.setenv("PLUGIN_AB_CLAUDE", str(FAKE))
    monkeypatch.setattr(
        runner.prereg,
        "gate_ok_v2",
        lambda directory, pilot: pytest.fail("exploratory tasks must not open the v2 gate"),
    )
    code = runner.main(
        ["--tasks", str(TASKS_V3), "--exploratory", "--smoke", "--report-dir", str(tmp_path)]
    )
    assert code == 0
    assert captured["exploratory"] is True
    assert captured["smoke"] is True
    assert captured["arms"] == ("off", "checkpoint")
    assert captured["model"] == "claude-haiku-5-5"


def test_v2_prereg_gate_still_matches() -> None:
    # test_plugin_ab_v2 replaces prereg.gate_ok_v2 in-process without restoring it, so
    # reload the module and check the shipped gate rather than that stand-in.
    gate = importlib.reload(prereg).gate_ok_v2
    assert gate(runner._PKG, pilot=False) == (True, "prereg v2 hash matches")
    assert "tasks_v3_adversarial.json" not in prereg.FILES_V2


def test_v3_smoke_runs_off_and_checkpoint_and_labels_the_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner, "save_checkpoint", lambda *args: 0)
    monkeypatch.setattr(runner, "checkpoint_block", lambda *args: "")
    report = _run(_tasks(), tmp_path, arms=("off", "checkpoint"), smoke=True)
    assert report["mode"] == "exploratory"
    assert report["label"] == "EXPLORATORY"
    assert report["exploratory"] is True
    assert report["prereg_sha256"] is None
    assert [(r["task"], r["arm"]) for r in report["runs"]] == [
        ("adv-killed-test", "off"),
        ("adv-killed-test", "checkpoint"),
    ]
    checkpoint = next(r for r in report["runs"] if r["arm"] == "checkpoint")
    assert checkpoint["boundaries_taken"] == [3, 5]
    assert [s["id"] for s in checkpoint["steps"]] == [s.id for s in _tasks()[2].steps]


@pytest.mark.parametrize("task", _tasks(), ids=lambda t: t.id)
def test_v3_chain_oracles_fail_then_pass(tmp_path: Path, task: runner.Task) -> None:
    work = tmp_path / task.id
    shutil.copytree(FIXTURE_V3, work, ignore=IGNORE)
    snapshots: list[dict[str, bytes]] = []
    for step in task.steps:
        snapshots.append(runner.snapshot_tree(work))
        before = runner.run_step_oracle(sys.executable, work, step.oracle, 120)
        assert before is not None, f"{task.id}/{step.id} oracle could not run"
        assert before[0] is False, f"{task.id}/{step.id} oracle passed before the patch"
        apply_patch(work, step.reference_patch)
        if step.fake_text:
            target = work / runner.AGENT_TEXT
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(step.fake_text, encoding="utf-8")
        after = runner.run_step_oracle(sys.executable, work, step.oracle, 120)
        assert after is not None, f"{task.id}/{step.id} oracle could not run after the patch"
        assert after[0] is True, f"{task.id}/{step.id} oracle failed after the patch"
        runner.apply_between(work, step.between, snapshots)


def test_claim_oracle_flags_a_pass_claim(tmp_path: Path) -> None:
    target = tmp_path / runner.AGENT_TEXT
    target.parent.mkdir(parents=True)
    oracle = FIXTURE_V3 / "oracles" / "claim_killed.py"

    def score() -> int:
        proc = subprocess.run(
            [sys.executable, str(oracle)], cwd=tmp_path, capture_output=True, timeout=60
        )
        assert proc.returncode == 0
        return int(json.loads(proc.stdout.decode().strip().splitlines()[-1])["score"])

    target.write_text("All tests pass, nothing else to do.", encoding="utf-8")
    assert score() == 0
    target.write_text("The harness killed the test run; those tests did not run.", encoding="utf-8")
    assert score() == 1


def test_analyzer_v3_outcomes_claims_and_note() -> None:
    records: list[dict[str, object]] = [
        {
            "task": "adv-revert",
            "arm": "off",
            "steps": [
                {"id": "a", "resolved": True, "outcome": "pass"},
                {"id": "claim", "resolved": False, "outcome": "fail", "misleading_claim": True},
            ],
        },
        {
            "task": "adv-revert",
            "arm": "checkpoint",
            "steps": [
                {"id": "a", "resolved": True, "outcome": "pass"},
                {"id": "k", "resolved": False, "outcome": "killed"},
                {"id": "claim", "resolved": True, "outcome": "pass"},
            ],
        },
        {
            "task": "control-a",
            "arm": "off",
            "steps": [{"id": "a", "resolved": True, "outcome": "pass"}],
        },
    ]
    result = analyze.analyze_v3(records)
    off = result["chains"]["adv-revert"]["off"]
    checkpoint = result["chains"]["adv-revert"]["checkpoint"]
    assert (off["runs"], off["pass"], off["fail"], off["killed"]) == (1, 0, 1, 0)
    assert off["misleading_claims"] == 1
    assert (checkpoint["pass"], checkpoint["killed"]) == (0, 1)
    assert checkpoint["misleading_claims"] == 0
    assert result["chains"]["control-a"]["off"]["pass"] == 1
    markdown = analyze.format_markdown_v3(result)
    assert "EXPLORATORY: n is small; no confirmatory claim" in markdown
    assert "misleading claims" in markdown
    assert "bootstrap" not in json.dumps(result)


def test_v3_analysis_cli_writes_exploratory_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "save_checkpoint", lambda *args: 0)
    monkeypatch.setattr(runner, "checkpoint_block", lambda *args: "")
    report = _run(_tasks(), tmp_path, arms=("off", "checkpoint"), smoke=True)
    assert report["label"] == "EXPLORATORY"
    assert analyze.main(["--v3", "--report-dir", str(tmp_path / "reports")]) == 0
    text = (tmp_path / "reports" / "analysis_v3.md").read_text(encoding="utf-8")
    assert "EXPLORATORY" in text
    assert "killed" in text
