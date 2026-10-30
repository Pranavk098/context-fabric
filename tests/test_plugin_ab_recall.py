"""Seeded recall facts, the ``no_restore`` control arm, and the 0-byte save guard (WP A3).

No model is called: ``tests/fake_claude.py`` stands in for ``claude``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from evals.plugin_ab import prices, runner

REPO = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).resolve().parent / "fake_claude.py"
TASKS_V2 = REPO / "evals" / "plugin_ab" / "tasks_v2.json"
FIXTURE_V2 = REPO / "evals" / "plugin_ab" / "fixture_v2"


def _long_checkout() -> runner.Task:
    return next(t for t in runner.load_tasks(TASKS_V2) if t.id == "long-checkout")


def _run(tmp_path: Path, arms: tuple[str, ...], seed: int = 7) -> dict[str, Any]:
    isolation = tmp_path / "iso"
    isolation.mkdir(exist_ok=True)
    return runner.run_experiment(
        [_long_checkout()],
        arms=arms,
        reps=0,
        seed=seed,
        model="haiku",
        claude_bin=str(FAKE),
        plugin_root=REPO,
        fixture=runner.DEFAULT_FIXTURE,
        prices=prices.load_prices(),
        python=sys.executable,
        fake=True,
        pilot=True,
        run_id="a3",
        report_dir=tmp_path / "reports",
        isolation=isolation,
    )


def _spy_commands(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    commands: list[list[str]] = []
    original = runner.build_command

    def spy(*args: object, **kwargs: object) -> list[str]:
        cmd = original(*args, **kwargs)  # type: ignore[arg-type]
        commands.append(cmd)
        return cmd

    monkeypatch.setattr(runner, "build_command", spy)
    return commands


def test_recall_facts_are_seeded_and_absent_from_the_repo() -> None:
    plant, questions = runner.make_recall_facts(7, "long-checkout", 0)
    again_plant, _ = runner.make_recall_facts(7, "long-checkout", 0)
    other_plant, other_questions = runner.make_recall_facts(8, "long-checkout", 0)
    assert plant == again_plant
    assert plant != other_plant
    assert [q.expected for q in questions] != [q.expected for q in other_questions]
    corpus = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in FIXTURE_V2.rglob("*")
        if p.is_file() and p.suffix in {".py", ".md", ".json", ".txt"}
    ) + TASKS_V2.read_text(encoding="utf-8")
    for q in questions:
        assert q.expected in plant
        assert q.expected not in corpus


def test_recall_score_requires_the_planted_values(tmp_path: Path) -> None:
    _, questions = runner.make_recall_facts(1, "t", 0)
    assert runner.score_recall(tmp_path, questions) == []  # no answers.json
    (tmp_path / "answers.json").write_text(
        '{"release_token": "unknown", "archive_days": "' + questions[1].expected + '"}',
        encoding="utf-8",
    )
    assert runner.score_recall(tmp_path, questions) == ["archive_days"]


def test_facts_are_planted_midsession_and_asked_after_the_break(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands = _spy_commands(monkeypatch)
    monkeypatch.setattr(runner, "save_checkpoint", lambda *a: 321)
    monkeypatch.setattr(runner, "checkpoint_block", lambda *a: "BLOCK")
    record = _run(tmp_path, ("checkpoint",))["runs"][0]
    plant, questions = runner.make_recall_facts(7, "long-checkout", -1)  # reps=0: warm-up only
    prompts = [cmd[cmd.index("-p") + 1] for cmd in commands]
    assert [i for i, p in enumerate(prompts) if questions[0].expected in p] == [
        runner.RECALL_PLANT_STEP
    ]
    assert plant in prompts[runner.RECALL_PLANT_STEP]
    assert "release_token" in prompts[-1]
    assert questions[0].expected not in prompts[-1]
    assert record["recall_score"] == 3 and record["recall_of"] == 3


def test_no_restore_control_scores_zero_and_is_valid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two-run pilot on fake claude: the checkpoint arm recalls, the control scores 0."""
    commands = _spy_commands(monkeypatch)
    monkeypatch.setattr(
        runner, "save_checkpoint", lambda *a: pytest.fail("control must not save a checkpoint")
    )
    monkeypatch.setattr(
        runner, "checkpoint_block", lambda *a: pytest.fail("control must not inject a block")
    )
    report = _run(tmp_path, (runner.RECALL_CONTROL_ARM,))
    (record,) = report["runs"]
    assert record["arm"] == "no_restore"
    assert record["recall_score"] == 0 and record["recall_of"] == 3
    assert record["recall_invalid_questions"] == []
    assert record["recall_valid"] is True
    assert record["boundaries_taken"] == [4, 8]
    assert {c["restore_outcome"] for c in record["checkpoints"]} == {"none"}
    assert all("--plugin-dir" not in cmd for cmd in commands)
    sessions = {cmd[cmd.index("--session-id") + 1] for cmd in commands if "--session-id" in cmd}
    assert len(sessions) == 3  # the session really breaks at both boundaries


def test_control_answering_a_question_is_flagged_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner, "score_recall", lambda work, questions: ["release_token"])
    (record,) = _run(tmp_path, (runner.RECALL_CONTROL_ARM,))["runs"]
    assert record["recall_invalid_questions"] == ["release_token"]
    assert record["recall_valid"] is False


def test_two_run_pilot_checkpoint_vs_control(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner, "save_checkpoint", lambda *a: 321)
    monkeypatch.setattr(runner, "checkpoint_block", lambda *a: "BLOCK")
    report = _run(tmp_path, ("checkpoint", runner.RECALL_CONTROL_ARM))
    scores = {r["arm"]: r["recall_score"] for r in report["runs"]}
    assert scores["checkpoint"] == 3
    assert scores["no_restore"] == 0


def test_zero_byte_save_is_an_infra_failure_not_degraded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands = _spy_commands(monkeypatch)
    monkeypatch.setattr(runner, "save_checkpoint", lambda *a: 0)
    monkeypatch.setattr(
        runner, "checkpoint_block", lambda *a: pytest.fail("no restore after an empty save")
    )
    (record,) = _run(tmp_path, ("checkpoint",))["runs"]
    assert record["infra_failure"] is True
    assert record["infra_reason"] == "save_empty"
    assert len(commands) == 4  # the chain stopped at the first boundary


def test_checkpoint_run_total_sums_both_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A new session must not turn the next step's delta negative or drop earlier cost."""
    monkeypatch.setattr(runner, "save_checkpoint", lambda *a: 321)
    monkeypatch.setattr(runner, "checkpoint_block", lambda *a: "BLOCK")
    task = next(t for t in runner.load_tasks(TASKS_V2) if t.id == "medium-refunds")
    isolation = tmp_path / "iso"
    isolation.mkdir()
    report = runner.run_experiment(
        [task],
        arms=("checkpoint",),
        reps=0,
        seed=0,
        model="haiku",
        claude_bin=str(FAKE),
        plugin_root=REPO,
        fixture=runner.DEFAULT_FIXTURE,
        prices=prices.load_prices(),
        python=sys.executable,
        fake=True,
        pilot=True,
        run_id="a3b",
        report_dir=tmp_path / "reports",
        isolation=isolation,
    )
    (record,) = report["runs"]
    assert record["boundaries_taken"] == [2]  # two sessions: steps 0-1, then step 2
    steps = record["steps"]
    for step in steps:
        for side in ("parent", "subagents"):
            assert all(v >= 0 for v in step["usage"][side].values() if isinstance(v, int | float))
        assert step["turns"] > 0 and step["dollars"] >= 0
    assert record["turns"] == sum(s["turns"] for s in steps)
    assert record["dollars"] == pytest.approx(sum(s["dollars"] for s in steps))
    for side in ("parent", "subagents"):
        for key, value in record["usage"][side].items():
            if isinstance(value, int | float):
                assert value == sum(s["usage"][side][key] for s in steps)
