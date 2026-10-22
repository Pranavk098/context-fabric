"""cfxd CLI tests: stop, status, and doctor against a subprocess daemon."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_cli(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cfxd", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _wait_for(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert path.exists(), f"{path} was not created in time"


def test_stop_removes_state_file(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    state_file = state_dir / "state.json"
    proc = subprocess.Popen(
        [sys.executable, "-m", "cfxd", "serve", "--state-dir", str(state_dir)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(state_file)
        result = _run_cli("stop", "--state-dir", str(state_dir))
        assert result.returncode == 0, result.stderr
        assert not state_file.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def test_status_not_running(tmp_path: Path) -> None:
    result = _run_cli("status", "--state-dir", str(tmp_path / "cfx"))
    assert result.returncode == 1
    assert "not running" in result.stdout


def test_doctor_no_state_exits_1(tmp_path: Path) -> None:
    result = _run_cli("doctor", "--state-dir", str(tmp_path / "cfx"))
    assert result.returncode == 1
    assert "no state file" in result.stdout
