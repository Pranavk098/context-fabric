"""MCP isolation in the plugin A/B harness: command flags, env, and churn detection."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from evals.plugin_ab import prices, runner

REPO = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).resolve().parent / "fake_claude.py"
TASKS_V2 = runner.DEFAULT_FIXTURE.parent / "tasks_v2.json"


@pytest.mark.parametrize("arm", ["off", "shipped", "checkpoint"])
def test_build_command_has_strict_empty_mcp_config(arm: str, tmp_path: Path) -> None:
    cfg = runner.write_empty_mcp_config(tmp_path)
    assert json.loads(cfg.read_text(encoding="utf-8")) == {"mcpServers": {}}
    cmd = runner.build_command(
        "claude",
        prompt="p",
        model="haiku",
        session_id="s",
        arm=arm,
        plugin_root=tmp_path,
        mcp_config=cfg,
    )
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--mcp-config") + 1] == str(cfg)


def _transcript(path: Path, deferred: int, instructions: int) -> None:
    lines = [{"type": "attachment", "attachment": {"type": "deferred_tools_delta"}}] * deferred
    lines += [{"type": "attachment", "attachment": {"type": "mcp_instructions_delta"}}] * (
        instructions
    )
    lines.append({"type": "assistant", "message": {}})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")


def test_mcp_churn_counts_per_invocation(tmp_path: Path) -> None:
    t = tmp_path / "projects" / "p" / "sid.jsonl"
    _transcript(t, 1, 1)
    assert runner.mcp_churn(tmp_path, "sid")[0] is False
    _transcript(t, 27, 0)
    assert runner.mcp_churn(tmp_path, "sid")[0] is True
    assert runner.mcp_churn(tmp_path, "sid", invocations=27)[0] is False
    _transcript(t, 0, 2)
    assert runner.mcp_churn(tmp_path, "sid")[0] is True


def test_churning_transcript_is_an_infra_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_run = subprocess.run

    def run_and_churn(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        proc = real_run(cmd, **kwargs)  # type: ignore[call-overload]
        env = kwargs["env"]
        sid = cmd[cmd.index("--session-id") + 1]
        root = Path(env["CLAUDE_CONFIG_DIR"]) / "projects"  # type: ignore[index]
        for path in root.glob(f"**/{sid}.jsonl"):
            with open(path, "a", encoding="utf-8") as fh:
                for _ in range(27):
                    fh.write(json.dumps({"attachment": {"type": "deferred_tools_delta"}}) + "\n")
        return proc

    monkeypatch.setattr(runner.subprocess, "run", run_and_churn)
    task = next(t for t in runner.load_tasks(TASKS_V2) if t.id == "medium-refunds")
    isolation = tmp_path / "iso"
    isolation.mkdir()
    report = runner.run_experiment(
        [task],
        arms=("off",),
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
        run_id="v2",
        report_dir=tmp_path / "reports",
        isolation=isolation,
    )
    record = report["runs"][0]
    assert record["infra_failure"] is True
    assert record["infra_reason"] == "mcp_churn"
