"""cfxd bound tests: allowlist, rewrite shape, receipts, stats, fast exit, fail-open."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from cfxd.bound import TAIL_LINES, handle_pre_tool_use, wrap_command
from cfxd.cli import main as cli_main
from cfxd.config import read_config, set_flag
from cfxd.handlers import clear, dispatch, register, register_after
from cfxd.stats import collect_stats, format_human

ROOT = Path(__file__).resolve().parents[1]
LAUNCH_SH = ROOT / "cfxd" / "launch.sh"


def _find_sh() -> str | None:
    found = shutil.which("sh")
    if found:
        return found
    if os.name == "nt":
        for candidate in (
            r"C:\Program Files\Git\bin\sh.exe",
            r"C:\Program Files\Git\usr\bin\sh.exe",
        ):
            if Path(candidate).exists():
                return candidate
    return None


SH = _find_sh()
# The wrapper uses `set -o pipefail` (Claude Code's Bash tool runs bash); dash rejects it.
BASH = SH if os.name == "nt" else shutil.which("bash")


@pytest.fixture(autouse=True)
def _clean() -> Any:
    clear()
    yield
    clear()


def _bash(command: str, **tool_input: Any) -> dict[str, Any]:
    return {
        "tool_name": "Bash",
        "session_id": "s1",
        "tool_use_id": "tu-1",
        "tool_input": {"command": command, **tool_input},
    }


def _on(tmp_path: Path) -> None:
    set_flag(tmp_path, "bound", True)


def _wrapped(command: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": {
                "command": f"set -o pipefail; ( {command} ) 2>&1 | tail -n {TAIL_LINES}"
            },
        }
    }


def test_pytest_rewritten_exact(tmp_path: Path) -> None:
    _on(tmp_path)
    assert handle_pre_tool_use(_bash("pytest tests/"), tmp_path) == _wrapped("pytest tests/")


def test_pipe_untouched(tmp_path: Path) -> None:
    _on(tmp_path)
    assert handle_pre_tool_use(_bash("pytest | head"), tmp_path) == {}


@pytest.mark.parametrize(
    "command",
    [
        "pytest && echo done",
        "pytest; ls",
        "pytest || true",
        "pytest > out.txt",
        "pytest < in.txt",
        "pytest $(date)",
        "pytest `date`",
        "pytest\nls",
        "pytest &",
        "pytest -q",
        "pytest --quiet",
        "pytest -- -k head",
        "ls -la",
        "git status",
    ],
)
def test_untouched_commands(tmp_path: Path, command: str) -> None:
    _on(tmp_path)
    assert handle_pre_tool_use(_bash(command), tmp_path) == {}


def test_git_log_untouched_with_count(tmp_path: Path) -> None:
    _on(tmp_path)
    assert handle_pre_tool_use(_bash("git log --oneline"), tmp_path) == _wrapped(
        "git log --oneline"
    )
    for command in ("git log -5", "git log -n 5", "git log --max-count=5", "git log -n5"):
        assert handle_pre_tool_use(_bash(command), tmp_path) == {}


@pytest.mark.parametrize(
    ("command", "key"),
    [
        ("pytest -x tests/", "pytest"),
        ("python -m pytest tests/", "pytest"),
        ("uv run pytest tests/", "pytest"),
        ("npx pytest tests/", "pytest"),
        ("npm test", "npm test"),
        ("npm run test -- --watch=false", "npm run test"),
        ("yarn test", "yarn test"),
        ("pnpm test", "pnpm test"),
        ("go test ./...", "go test"),
        ("cargo test", "cargo test"),
        ("dotnet test", "dotnet test"),
        ("mvn test", "mvn test"),
        ("gradle test", "gradle test"),
        ("make test", "make test"),
        ("docker logs api", "docker logs"),
        ("kubectl logs pod", "kubectl logs"),
        ("journalctl -u api", "journalctl"),
    ],
)
def test_allowlist_keys(tmp_path: Path, command: str, key: str) -> None:
    _on(tmp_path)
    assert handle_pre_tool_use(_bash(command), tmp_path) == _wrapped(command)
    receipt = json.loads((tmp_path / "bound.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert receipt["key"] == key


def test_input_fields_preserved(tmp_path: Path) -> None:
    _on(tmp_path)
    tool_input = {
        "command": "go test ./...",
        "timeout": 600_000,
        "description": "run the tests",
        "run_in_background": False,
    }
    payload = {
        "tool_name": "Bash",
        "session_id": "s1",
        "tool_use_id": "tu-1",
        "tool_input": tool_input,
    }
    updated = handle_pre_tool_use(payload, tmp_path)["hookSpecificOutput"]["updatedInput"]
    expected = _wrapped(tool_input["command"])["hookSpecificOutput"]["updatedInput"]["command"]
    assert updated == {**tool_input, "command": expected}
    for field in ("timeout", "description", "run_in_background"):
        assert json.dumps(updated[field]) == json.dumps(tool_input[field])


def test_background_untouched(tmp_path: Path) -> None:
    _on(tmp_path)
    assert handle_pre_tool_use(_bash("pytest tests/", run_in_background=True), tmp_path) == {}


def test_setting_off_untouched(tmp_path: Path) -> None:
    assert handle_pre_tool_use(_bash("pytest tests/"), tmp_path) == {}


def test_env_opt_out_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _on(tmp_path)
    monkeypatch.setenv("CFX_BOUND", "0")
    assert handle_pre_tool_use(_bash("pytest tests/"), tmp_path) == {}


def test_non_bash_tool_untouched(tmp_path: Path) -> None:
    _on(tmp_path)
    payload = {"tool_name": "Read", "tool_input": {"command": "pytest tests/"}}
    assert handle_pre_tool_use(payload, tmp_path) == {}


def test_malformed_payload_fails_open(tmp_path: Path) -> None:
    _on(tmp_path)
    for payload in (
        {"tool_name": "Bash"},
        {"tool_name": "Bash", "tool_input": "not-a-dict"},
        {"tool_name": "Bash", "tool_input": {"command": None}},
        {"tool_name": "Bash", "tool_input": {"command": 42}},
    ):
        assert handle_pre_tool_use(payload, tmp_path) == {}


@pytest.mark.skipif(BASH is None, reason="bash is not available")
def test_wrapper_keeps_exit_and_last_150_lines(tmp_path: Path) -> None:
    script = tmp_path / "noisy.sh"
    script.write_text(
        'i=1\nwhile [ $i -le 300 ]; do echo "line $i"; i=$((i+1)); done\n'
        'echo "to-stderr" >&2\nexit 3\n',
        encoding="utf-8",
    )
    proc = subprocess.run(
        [str(BASH), "-c", wrap_command(f"sh '{script.as_posix()}'")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 3
    lines = proc.stdout.splitlines()
    assert len(lines) == 150
    assert lines[0] == "line 152"
    assert lines[-2:] == ["line 300", "to-stderr"]


def test_receipt_is_content_free(tmp_path: Path) -> None:
    _on(tmp_path)
    canary = "CANARY-9f8e7d6c5b4a3210-NEVER-STORE"
    command = f"pytest --canary {canary}"
    assert handle_pre_tool_use(_bash(command), tmp_path) != {}
    text = (tmp_path / "bound.jsonl").read_text(encoding="utf-8")
    assert canary not in text
    assert "pytest --canary" not in text
    receipt = json.loads(text.splitlines()[-1])
    assert receipt["event"] == "bound"
    assert receipt["key"] == "pytest"
    assert receipt["command_len"] == len(command)
    assert receipt["tail_lines"] == TAIL_LINES
    assert receipt["session_id"] == "s1"


def test_stats_counts_bounded(tmp_path: Path) -> None:
    _on(tmp_path)
    handle_pre_tool_use(_bash("pytest a"), tmp_path)
    handle_pre_tool_use(_bash("go test ./..."), tmp_path)
    stats = collect_stats(tmp_path)
    assert stats["bounded"] == 2
    assert "Bounded:" in format_human(stats)


def test_set_bound_creates_and_removes_flag(tmp_path: Path) -> None:
    assert cli_main(["set", "bound", "on", "--state-dir", str(tmp_path)]) == 0
    assert (tmp_path / "bound.on").exists()
    assert read_config(tmp_path)["bound"] is True
    assert cli_main(["set", "bound", "off", "--state-dir", str(tmp_path)]) == 0
    assert not (tmp_path / "bound.on").exists()
    assert read_config(tmp_path)["bound"] is False


@pytest.mark.skipif(SH is None, reason="sh is not available")
def test_launch_fast_exit_when_flag_absent(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    state_dir.mkdir()
    payload = json.dumps(
        {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": "pytest"}}
    )
    proc = subprocess.run(
        [str(SH), str(LAUNCH_SH), "forward", "PreToolUse"],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env={**os.environ, "CFX_HOME": str(state_dir)},
        timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert not (state_dir / "state.json").exists()


def test_register_after_keeps_existing_handler(tmp_path: Path) -> None:
    register("PreToolUse", lambda payload: {"first": True})
    register_after("PreToolUse", lambda payload: {"second": True})
    assert dispatch("PreToolUse", {}) == {"first": True}
    clear()
    register("PreToolUse", lambda payload: {})
    register_after("PreToolUse", lambda payload: {"second": True})
    assert dispatch("PreToolUse", {}) == {"second": True}


def test_test_key_any_finds_runners_in_compound_commands() -> None:
    from cfxd.bound import test_key_any

    assert (
        test_key_any("cd x && uv run --python 3.12 python -m pytest -q tests 2>&1 | tail -3")
        == "pytest"
    )
    assert test_key_any("git status; npm test") == "npm test"
    assert test_key_any("cd y && go test ./...") == "go test"
    assert test_key_any("ls | grep pytest") is None
    assert test_key_any("echo pytest is great") is None
    assert test_key_any(42) is None  # type: ignore[arg-type]
