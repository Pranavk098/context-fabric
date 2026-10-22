"""cfxd shim tests: the shim fails open under every failure mode."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from cfxd.daemon import Daemon, write_state
from cfxd.handlers import clear, echo, register

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "cfxd" / "shim.py"


def _run_shim(state_dir: Path, payload: bytes, event: str = "PreToolUse") -> tuple[int, str, float]:
    env = {**os.environ, "CFX_HOME": str(state_dir)}
    start = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-I", "-S", str(SHIM), event],
        input=payload,
        capture_output=True,
        env=env,
        timeout=30,
    )
    return proc.returncode, proc.stdout.decode("utf-8"), time.perf_counter() - start


def test_shim_no_state_file_prints_empty(tmp_path: Path) -> None:
    rc, out, elapsed = _run_shim(tmp_path / "missing", b"{}")
    assert rc == 0
    assert out.strip() == "{}"
    assert elapsed < 1.0


def test_shim_dead_daemon_prints_empty(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    d = Daemon(state_dir, 0)
    d.start()
    port = d.port
    d.stop()
    # Re-write a stale state file pointing at the now-dead port.
    write_state(state_dir, port, os.getpid())
    rc, out, elapsed = _run_shim(state_dir, b"{}")
    assert rc == 0
    assert out.strip() == "{}"
    assert elapsed < 1.0


def test_shim_forwards_to_daemon(tmp_path: Path) -> None:
    clear()
    state_dir = tmp_path / "cfx"
    d = Daemon(state_dir, 0)
    d.start()
    try:
        register("PreToolUse", echo)
        rc, out, _ = _run_shim(state_dir, b'{"tool_name": "Bash"}')
        assert rc == 0
        assert json.loads(out) == {"tool_name": "Bash"}
    finally:
        d.stop()
        clear()
