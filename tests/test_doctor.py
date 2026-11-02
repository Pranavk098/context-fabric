"""W2-02: the deep ``cfxd doctor`` checks and the structured hook log.

Every check is asserted twice where it matters: the pass line, and the fail line with
its exact fix command. The doctor output must stay paste-safe, so the render test
asserts the state-dir path never appears. The log tests read the real ``events.jsonl``
written by an in-process daemon.
"""

from __future__ import annotations

import http.client
import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cfxd import checkup
from cfxd.daemon import Daemon
from cfxd.handlers import clear, register

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def daemon(tmp_path: Path) -> Iterator[Daemon]:
    clear()
    d = Daemon(tmp_path / "cfx", 0)
    d.start()
    try:
        yield d
    finally:
        d.stop()
        clear()


def _post(daemon: Daemon, event: str, payload: bytes, token: str | None = None) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=2)
    try:
        headers = {"Content-Type": "application/json"}
        headers["X-Cfx-Token"] = token if token is not None else daemon.token
        conn.request("POST", f"/hooks/{event}", body=payload, headers=headers)
        resp = conn.getresponse()
        resp.read()  # drain the body so the server sees a clean close
        return resp.status
    finally:
        conn.close()


def _events(daemon: Daemon) -> list[dict[str, Any]]:
    text = daemon.events_path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _fake_claude(version: str = "2.1.0 (Claude Code)") -> Any:
    return lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=version + "\n")


def _hooks_root(tmp_path: Path, events: list[str] | None = None) -> Path:
    root = tmp_path / "plugin"
    (root / "hooks").mkdir(parents=True)
    declared = events if events is not None else list(checkup._REQUIRED_HOOKS)
    (root / "hooks" / "hooks.json").write_text(
        json.dumps({"hooks": {name: [] for name in declared}}), encoding="utf-8"
    )
    return root


# --- the six checks ---------------------------------------------------------------


def test_daemon_check_passes_against_a_live_helper(daemon: Daemon) -> None:
    check = checkup._daemon_check(daemon.state_dir)
    assert check.ok
    assert f"127.0.0.1:{daemon.port}" in check.detail
    assert "identity verified" in check.detail


def test_daemon_check_fails_without_a_state_file(tmp_path: Path) -> None:
    check = checkup._daemon_check(tmp_path / "cfx")
    assert not check.ok
    assert "no state file" in check.detail
    assert check.fix == "python -m cfxd serve"


def test_token_check_missing_empty_and_permissioned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "cfx"
    state.mkdir()
    missing = checkup._token_check(state)
    assert not missing.ok and "missing" in missing.detail
    assert missing.fix is not None and "cfxd serve" in missing.fix

    (state / "token").write_text("   \n", encoding="utf-8")
    assert "empty" in checkup._token_check(state).detail

    (state / "token").write_text("abc", encoding="utf-8")
    monkeypatch.setattr(checkup, "token_permission_warning", lambda _path: None)
    assert checkup._token_check(state).ok

    monkeypatch.setattr(checkup, "token_permission_warning", lambda _path: "readable")
    failed = checkup._token_check(state)
    assert not failed.ok and "readable by other users" in failed.detail
    assert failed.fix == checkup._token_fix()


@pytest.mark.skipif(os.name == "nt", reason="Windows ACLs, not POSIX modes, gate the token")
def test_token_check_flags_a_world_readable_file(tmp_path: Path) -> None:
    state = tmp_path / "cfx"
    state.mkdir()
    token = state / "token"
    token.write_text("abc", encoding="utf-8")
    token.chmod(0o644)
    check = checkup._token_check(state)
    assert not check.ok
    assert check.fix == 'chmod 600 "$CFX_HOME/token"'


def test_hooks_check_all_present_and_one_missing(tmp_path: Path) -> None:
    assert checkup._hooks_check(_hooks_root(tmp_path)).ok

    partial = _hooks_root(tmp_path / "b", ["SessionStart", "PreToolUse"])
    failed = checkup._hooks_check(partial)
    assert not failed.ok
    assert "PreCompact" in failed.detail and "PostToolUse" in failed.detail
    assert failed.fix == "claude plugin install context-fabric@context-fabric"

    empty = tmp_path / "c"
    empty.mkdir()
    assert not checkup._hooks_check(empty).ok


def test_state_dir_check_writable_and_content_classes(tmp_path: Path) -> None:
    state = tmp_path / "cfx"
    state.mkdir()
    assert checkup._state_dir_check(state).ok

    (state / "notes.txt").write_text("undeclared", encoding="utf-8")
    failed = checkup._state_dir_check(state)
    assert not failed.ok
    assert "1 file(s) exceed their content class" in failed.detail
    assert failed.fix == "python scripts/state_audit.py"

    missing = checkup._state_dir_check(tmp_path / "absent")
    assert not missing.ok and "not writable" in missing.detail


def test_claude_code_check_reports_version_and_fails_closed() -> None:
    found = checkup._claude_code_check(_fake_claude("2.1.0 (Claude Code)"))
    assert found.ok and found.detail == "version 2.1.0 (Claude Code)"

    def missing(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("claude")

    absent = checkup._claude_code_check(missing)
    assert not absent.ok and absent.fix == checkup._claude_install()

    failed = checkup._claude_code_check(lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    assert not failed.ok


def test_launcher_check_windows_needs_sh_and_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkup, "os", SimpleNamespace(name="nt"))

    assert checkup._launcher_check(lambda name: None).fix is not None
    assert "git-scm.com" in checkup._launcher_check(lambda name: None).fix  # type: ignore[operator]
    python_missing = checkup._launcher_check(lambda name: None if name == "python" else "sh")
    assert not python_missing.ok and "python" in python_missing.detail
    assert checkup._launcher_check(lambda name: f"/usr/bin/{name}").ok


def test_launcher_check_is_not_needed_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkup, "os", SimpleNamespace(name="posix"))
    check = checkup._launcher_check(lambda name: None)
    assert check.ok and "not needed" in check.detail


def test_doctor_runs_six_checks_in_order_without_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "cfx"
    state.mkdir()
    (state / "token").write_text("abc", encoding="utf-8")
    monkeypatch.setattr(checkup, "token_permission_warning", lambda _path: None)
    checks = checkup.doctor(
        state,
        plugin_root=_hooks_root(tmp_path),
        runner=_fake_claude(),
        which=lambda name: f"/usr/bin/{name}",
    )
    assert [check.name for check in checks] == [
        "daemon",
        "token",
        "hooks",
        "state dir",
        "claude code",
        "launcher",
    ]
    text = checkup.format_doctor(checks)
    assert "checks (paste-safe" in text
    assert "FAIL  no state file" in text
    assert "PASS  present; readable only by you" in text
    assert str(tmp_path) not in text


# --- the structured hook log ------------------------------------------------------


def test_hook_line_has_decision_reason_bytes_and_segment(daemon: Daemon) -> None:
    register("PreToolUse", lambda _payload: {"hookSpecificOutput": {"permissionDecision": "allow"}})
    assert _post(daemon, "PreToolUse", b'{"session_id": "s1"}') == 200
    entry = _events(daemon)[-1]
    assert entry["event"] == "PreToolUse"
    assert entry["decision"] == "fired"
    assert entry["reason"] == "responded"
    assert entry["segment"] == 0
    assert entry["bytes_in"] == len(b'{"session_id": "s1"}')
    assert isinstance(entry["bytes_out"], int) and entry["bytes_out"] > 0
    assert isinstance(entry["latency_ms"], float)


def test_hook_line_marks_an_empty_answer_as_skipped(daemon: Daemon) -> None:
    assert _post(daemon, "Stop", b'{"session_id": "s2"}') == 200
    entry = _events(daemon)[-1]
    assert entry["decision"] == "skipped"
    assert entry["reason"] == "no-response"


def test_precompact_starts_the_next_segment(daemon: Daemon) -> None:
    _post(daemon, "PreToolUse", b'{"session_id": "s3"}')
    _post(daemon, "PreCompact", b'{"session_id": "s3"}')
    _post(daemon, "PreToolUse", b'{"session_id": "s3"}')
    _post(daemon, "PreCompact", b'{"session_id": "s3"}')
    _post(daemon, "PostToolUse", b'{"session_id": "s3"}')
    assert [entry["segment"] for entry in _events(daemon)] == [0, 0, 1, 1, 2]


def test_rejected_hook_line_carries_its_reason_code(daemon: Daemon) -> None:
    assert _post(daemon, "PreToolUse", b"{}", token="wrong") == 401
    entry = _events(daemon)[-1]
    assert entry["decision"] == "rejected"
    assert entry["reason"] == "token"
    assert entry["segment"] is None


def test_events_log_never_exceeds_the_hard_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cap = 500
    monkeypatch.setattr("cfxd.daemon.EVENTS_ROTATE_BYTES", cap)
    d = Daemon(tmp_path / "cfx", 0)
    d.start()
    try:
        for index in range(40):
            d.log_event({"ts": "x", "event": "e", "n": index, "pad": "x" * 60})
            assert d.events_path.stat().st_size <= cap
    finally:
        d.stop()
    state = tmp_path / "cfx"
    assert (state / "events.jsonl.1").exists()
    assert not (state / "events.jsonl.2").exists()
