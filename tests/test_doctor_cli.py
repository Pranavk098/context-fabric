"""W2-06: ``cfxd doctor`` prints the deep checks first, then daemon health, paste-safely."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from cfxd import cli
from cfxd.checkup import DoctorCheck

CANARY = "canary-user-7c1d"
FIX = "python -m cfxd serve"


def _passing_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck("daemon", True, "reachable on 127.0.0.1:1 (version test), identity verified"),
        DoctorCheck("token", True, "present; readable only by you"),
        DoctorCheck("hooks", True, "5 of 5 events registered"),
        DoctorCheck("state dir", True, "writable; 0 content-class violations"),
        DoctorCheck("claude code", True, "version 2.1.0"),
        DoctorCheck("launcher", True, "sh found (Git for Windows); python fallback available"),
    ]


def _reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A daemon that answers every call, so the health/latency lines print."""
    monkeypatch.setattr(
        cli, "read_state", lambda path: {"port": 47831, "pid": 1, "version": "test"}
    )
    monkeypatch.setattr(cli, "get_health", lambda port: (200, b'{"ok": true}'))
    monkeypatch.setattr(cli, "post_hook", lambda port, event, body, token=None: (200, b""))
    monkeypatch.setattr(cli, "_measure_shim", lambda state_dir, runs: ([1.0, 2.0], 0))
    monkeypatch.setattr(cli, "verify_identity", lambda state_dir, port: True)
    monkeypatch.setattr(
        cli, "read_config", lambda state_dir: {"brief": False, "carry": False, "fold": False}
    )
    monkeypatch.setattr(cli, "coverage", lambda *args, **kwargs: {"delivery": None})
    monkeypatch.setattr(
        cli, "waste", lambda *args, **kwargs: {"mcp_churn": {"sessions_with_churn": 0}}
    )


def test_all_checks_pass_exits_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "deep_doctor", lambda state_dir: _passing_checks())
    _reachable(monkeypatch)
    code = cli._cmd_doctor(argparse.Namespace(state_dir=str(tmp_path / "cfx")))
    out = capsys.readouterr().out
    assert code == 0
    assert out.index("checks (paste-safe") < out.index("health:      ok")
    assert "identity:    verified" in out
    assert "cfxd doctor: healthy" in out


def test_failing_check_exits_1_and_prints_its_fix(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    checks = _passing_checks()
    checks[2] = DoctorCheck("hooks", False, "not registered: Stop", FIX)
    monkeypatch.setattr(cli, "deep_doctor", lambda state_dir: checks)
    _reachable(monkeypatch)
    code = cli._cmd_doctor(argparse.Namespace(state_dir=str(tmp_path / "cfx")))
    out = capsys.readouterr().out
    assert code == 1
    assert f"FAIL  not registered: Stop; fix: {FIX}" in out
    assert "cfxd doctor: unhealthy" in out


def test_daemon_down_prints_checks_before_the_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    checks = [DoctorCheck("daemon", False, "no state file; the local helper is not running", FIX)]
    monkeypatch.setattr(cli, "deep_doctor", lambda state_dir: checks)
    monkeypatch.setattr(cli, "read_state", lambda path: None)
    code = cli._cmd_doctor(argparse.Namespace(state_dir=str(tmp_path / "cfx")))
    out = capsys.readouterr().out
    assert code == 1
    assert "checks (paste-safe" in out
    assert "no state file" in out
    assert "daemon not running: start Claude Code, or run: python -m cfxd serve" in out


def test_home_canary_never_appears(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    home = tmp_path / CANARY
    state_dir = home / ".cfx" / "daemon"
    state_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(cli, "read_state", lambda path: None)
    monkeypatch.setattr(
        cli,
        "deep_doctor",
        lambda state_dir: [DoctorCheck("daemon", False, "no state file", FIX)],
    )
    code = cli.main(["doctor", "--state-dir", str(state_dir)])
    out = capsys.readouterr().out
    assert code == 1
    assert "~/.cfx/daemon/state.json" in out
    assert CANARY not in out
    assert str(home) not in out


def test_state_dir_option_is_used(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CFX_HOME", str(tmp_path / "ignored"))
    state_dir = tmp_path / "chosen"
    seen: dict[str, Path] = {}

    def fake_read_state(path: Path) -> None:
        seen["path"] = path
        return None

    monkeypatch.setattr(cli, "read_state", fake_read_state)
    monkeypatch.setattr(cli, "deep_doctor", lambda state_dir: _passing_checks())
    code = cli.main(["doctor", "--state-dir", str(state_dir)])
    assert seen["path"] == state_dir / "state.json"
    assert code == 0
