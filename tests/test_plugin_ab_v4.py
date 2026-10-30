"""v4 delegation harness (WP H2): arms, parent prompt, hidden/planted evaluation, no_spawn.

No model is called: ``tests/fake_claude_v4.py`` stands in for ``claude`` and the real hidden
tests run against the copied v4 fixture.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from evals.plugin_ab import prices, runner, v4_prompt

REPO = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).resolve().parent / "fake_claude_v4.py"
TASK_ID = "v4-echo-sep"


def _tasks() -> list[runner.Task]:
    return runner.load_tasks(runner.TASKS_V4)


def _task() -> runner.Task:
    return next(t for t in _tasks() if t.id == TASK_ID)


def _run(
    tmp_path: Path, arms: tuple[str, ...], reps: int = 1, max_runs: int | None = None
) -> dict[str, Any]:
    isolation = tmp_path / "iso"
    isolation.mkdir(exist_ok=True)
    return runner.run_experiment(
        [_task()],
        arms=arms,
        reps=reps,
        seed=3,
        model="sonnet",
        claude_bin=str(FAKE),
        plugin_root=REPO,
        fixture=runner.DEFAULT_FIXTURE,
        prices=prices.load_prices(),
        python=sys.executable,
        fake=True,
        pilot=True,
        run_id="h2",
        report_dir=tmp_path / "reports",
        isolation=isolation,
        max_runs=max_runs,
    )


@pytest.fixture(scope="module")
def all_arms(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any]]:
    report = _run(tmp_path_factory.mktemp("v4"), runner.V4_ARMS)
    return {r["arm"]: r for r in report["runs"]}


def test_v4_task_file_loads_and_is_not_v2() -> None:
    tasks = _tasks()
    assert len(tasks) == 8
    assert runner.is_v4(tasks) and not runner.is_v2(tasks) and not runner.is_v3(tasks)
    assert all(t.planted_facts and t.hidden_tests and t.timeout for t in tasks)
    assert not runner.is_v4(runner.load_tasks(runner.TASKS_V2))


@pytest.mark.parametrize(
    ("arm", "brief", "v1"),
    [
        ("none", False, False),
        ("brief_v0", True, False),
        ("brief_v1", True, True),
        ("fork", False, False),
        ("control", False, False),
    ],
)
def test_arm_config(arm: str, brief: bool, v1: bool, tmp_path: Path) -> None:
    runner._write_arm_config(arm, tmp_path)
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert (cfg["brief"], cfg["brief_v1"], cfg["carry_back"]) == (brief, v1, False)


def test_non_v4_arms_write_no_config(tmp_path: Path) -> None:
    for arm in ("off", "shipped", "checkpoint"):
        runner._write_arm_config(arm, tmp_path)
    assert not (tmp_path / "config.json").exists()


def test_parent_prompt_names_agent_tool_and_child_model_and_withholds_facts_from_control() -> None:
    task = _task()
    for arm in ("none", "brief_v0", "brief_v1"):
        text = v4_prompt.parent_prompt(task.prompt, arm, "haiku")
        assert "Agent tool" in text and '"haiku"' in text and "must delegate" in text
        assert "DEFAULT_ECHO_SEP" in text  # planted facts live in the parent prompt
    fork = v4_prompt.parent_prompt(task.prompt, "fork", "haiku")
    assert "fork" in fork and '"haiku"' not in fork and "DEFAULT_ECHO_SEP" in fork
    control = v4_prompt.parent_prompt(task.prompt, "control", "haiku")
    assert "DEFAULT_ECHO_SEP" not in control and "sep must be a string" not in control
    assert "Agent tool" in control and "must delegate" in control
    assert control.startswith(v4_prompt.task_line(task.prompt))


def test_every_task_has_a_task_line_without_planted_facts() -> None:
    for task in _tasks():
        line = v4_prompt.task_line(task.prompt)
        assert line and v4_prompt.CONSTRAINTS_MARKER not in line


def test_plan_v4_has_no_warmups_and_is_seeded_abba() -> None:
    tasks = _tasks()
    a = runner.plan_v4(tasks, runner.V4_ARMS, 2, 5)
    assert a == runner.plan_v4(tasks, runner.V4_ARMS, 2, 5)
    assert not any(s.warm_up for s in a)
    assert [s.order_index for s in a] == list(range(len(a)))
    assert len(a) == len(tasks) * len(runner.V4_ARMS) * 2
    first_task = [s.arm for s in a if s.task_id == a[0].task_id]
    assert first_task[:5] == list(reversed(first_task[5:10]))  # rep 1 reverses rep 0


def test_command_keeps_mcp_isolation_and_caps_for_every_arm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[list[str]] = []
    original = runner.build_command

    def spy(*args: object, **kwargs: object) -> list[str]:
        cmd = original(*args, **kwargs)  # type: ignore[arg-type]
        commands.append(cmd)
        return cmd

    monkeypatch.setattr(runner, "build_command", spy)
    monkeypatch.setenv("EVALS_MAX_RUN_USD", "0.75")
    _run(tmp_path, runner.V4_ARMS, reps=1)
    assert len(commands) == len(runner.V4_ARMS)
    for cmd in commands:
        assert "--strict-mcp-config" in cmd and "--plugin-dir" in cmd
        assert cmd[cmd.index("--max-budget-usd") + 1] == "0.75"
        assert cmd[cmd.index("--max-turns") + 1] == str(runner.V4_PARENT_MAX_TURNS)
        assert cmd[cmd.index("--model") + 1] == "sonnet"


@pytest.mark.parametrize("arm", runner.V4_ARMS)
def test_every_arm_produces_a_complete_record(
    arm: str, all_arms: dict[str, dict[str, Any]]
) -> None:
    rec = all_arms[arm]
    assert not rec.get("infra_failure")
    for key in (
        "hidden_pass",
        "planted",
        "planted_passed",
        "planted_of",
        "spawns",
        "no_spawn",
        "agent_calls",
        "child_compactions",
        "child_overflows",
        "child_rereads",
        "child_parent_msg_overlap",
        "receipts",
        "dollars",
        "usage",
        "cap_hit",
        "cap_usd",
        "parent_model",
    ):
        assert key in rec, key
    assert rec["v4"] and not rec["warm_up"] and rec["parent_model"] == "sonnet"
    assert set(rec["planted"]) == {f.id for f in _task().planted_facts}
    assert rec["spawns"] == 1 and rec["no_spawn"] is False and rec["agent_calls"] == 1
    assert rec["usage"]["parent"]["turns"] >= 2 and rec["usage"]["subagents"]["turns"] >= 2
    assert rec["dollars"] is not None and rec["dollars"] > 0
    assert rec["cap_hit"] is False
    assert rec["child_models"]
    blob = json.dumps(rec)
    assert "Constraints" not in blob and str(REPO) not in blob  # counts and ids only


@pytest.mark.parametrize("arm", ["none", "brief_v0", "brief_v1", "fork"])
def test_arms_with_the_facts_pass_hidden_and_planted_tests(
    arm: str, all_arms: dict[str, dict[str, Any]]
) -> None:
    rec = all_arms[arm]
    assert rec["hidden_pass"] is True
    assert all(rec["planted"].values()) and rec["planted_passed"] == rec["planted_of"] == 2
    assert rec["resolved"] is True and rec["facts_in_prompt"] is True


def test_control_passes_behaviour_but_fails_every_planted_fact(
    all_arms: dict[str, dict[str, Any]],
) -> None:
    rec = all_arms["control"]
    assert rec["hidden_pass"] is True
    assert rec["planted"] and not any(rec["planted"].values())
    assert rec["planted_passed"] == 0 and rec["resolved"] is False
    assert rec["facts_in_prompt"] is False


def test_brief_arms_join_r1_receipts(all_arms: dict[str, dict[str, Any]]) -> None:
    for arm in ("none", "fork", "control"):
        assert all_arms[arm]["receipts"] == [] and all_arms[arm]["brief_fired"] is False
    v0, v1 = all_arms["brief_v0"], all_arms["brief_v1"]
    for rec in (v0, v1):
        assert rec["brief_fired"] is True and len(rec["receipts"]) == 1
        receipt = rec["receipts"][0]
        assert receipt["matched"] is True and receipt["join"] == "tool_use_id"
        assert receipt["decision"] == "briefed" and receipt["packet_bytes"] > 0
        assert "briefed_files" not in receipt and "child_files_read" not in receipt
    assert v0["receipts"][0]["n_rereads_of_briefed"] == 1  # the pointer list is re-read anyway
    assert v1["receipts"][0]["n_rereads_of_briefed"] == 0


def test_child_rereads_compactions_and_fork_replay_are_counted(
    all_arms: dict[str, dict[str, Any]],
) -> None:
    assert all_arms["none"]["child_rereads"] == 1
    assert all_arms["brief_v1"]["child_rereads"] == 0
    assert all_arms["none"]["child_compactions"] == 0
    assert all_arms["fork"]["child_compactions"] == 1
    assert all_arms["fork"]["child_parent_msg_overlap"] >= 1
    assert all_arms["none"]["child_parent_msg_overlap"] == 0


def test_no_spawn_is_recorded_as_data_not_infra(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PLUGIN_AB_V4_NOSPAWN", "1")
    report = _run(tmp_path, ("brief_v1",), reps=1)
    rec = report["runs"][0]
    assert not rec.get("infra_failure")
    assert rec["no_spawn"] is True and rec["spawns"] == 0 and rec["agent_calls"] == 0
    assert rec["child_models"] == [] and rec["hidden_pass"] is True
    assert rec["usage"]["subagents"]["turns"] == 0


def test_mcp_churn_is_still_an_infra_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner, "mcp_churn", lambda *a, **k: (True, (9, 9)))
    report = _run(tmp_path, ("none",), reps=1, max_runs=1)
    rec = report["runs"][0]
    assert rec["infra_failure"] is True and rec["infra_reason"] == "mcp_churn"


def test_report_marks_pilot_and_child_model(tmp_path: Path) -> None:
    report = _run(tmp_path, ("none",), reps=1, max_runs=1)
    assert report["mode"] == "pilot" and report["child_model"] == "haiku"
    assert report["prereg_sha256"] is None
    assert (tmp_path / "reports" / "h2.json").is_file()


def test_main_requires_v4_flag_and_pilot(capsys: pytest.CaptureFixture[str]) -> None:
    assert runner.main(["--tasks", str(runner.TASKS_V4)]) == 2
    assert "--v4" in capsys.readouterr().err
    assert runner.main(["--tasks", str(runner.TASKS_V4), "--v4"]) == 2
    assert "--pilot" in capsys.readouterr().err
    assert runner.main(["--tasks", str(runner.DEFAULT_TASKS), "--v4", "--pilot"]) == 2
    assert (
        runner.main(["--tasks", str(runner.TASKS_V4), "--v4", "--pilot", "--arms", "off,none"]) == 2
    )
    assert "unknown v4 arm" in capsys.readouterr().err
