"""P3: every state file has a content class, and startup enforces the class and the token file.

``content_class.audit_state_dir`` is the at-rest rule from the technical plan: counts-only
files may hold short single-line fields, snapshots and packets may hold bounded prose, the
retired ``obs/`` store is forbidden, and an undeclared file is a failure. The daemon startup
checks add the bind guard, the token-permission warning, and the legacy ``obs/`` migration.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import cfxd.carry as carry_mod
from cfxd import session_state as ss
from cfxd.content_class import (
    COUNTS,
    FORBIDDEN,
    INDEX,
    OPAQUE,
    SECRET,
    SNAPSHOT,
    UNDECLARED,
    audit_state_dir,
    classify,
)
from cfxd.daemon import (
    OBS_PURGE_FILE,
    Daemon,
    require_loopback_bind,
    token_permission_warning,
)
from cfxd.handlers import clear

_DOCS_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_docs_numbers.py"


def _load_docs_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_docs_numbers_w102", _DOCS_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DOCS = _load_docs_checker()
POSIX = os.name != "nt"


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    ss.reset()
    ss.set_range_recording(False)
    clear()
    yield
    ss.reset()
    ss.set_range_recording(False)
    clear()


@pytest.fixture
def daemon(tmp_path: Path) -> Iterator[Daemon]:
    d = Daemon(tmp_path / "cfx", 0)
    d.start()
    try:
        yield d
    finally:
        d.stop()


# --------------------------------------------------------------------- inventory


@pytest.mark.parametrize(
    ("rel", "name"),
    [
        ("token", SECRET.name),
        (".obs-purged", SECRET.name),
        ("state.json", COUNTS.name),
        ("config.json", COUNTS.name),
        ("events.jsonl", COUNTS.name),
        ("events.jsonl.1", COUNTS.name),
        ("briefs.jsonl", COUNTS.name),
        ("carry.jsonl", COUNTS.name),
        ("history/week-2026-11-01.json", COUNTS.name),
        ("carry/s1.json", SNAPSHOT.name),
        ("checkpoint/abc.json", SNAPSHOT.name),
        ("briefs/ab12/tu-1.json", INDEX.name),
        ("python", INDEX.name),
        ("daemon.log", OPAQUE.name),
    ],
)
def test_classify_declares_each_state_file(rel: str, name: str) -> None:
    klass = classify(Path(rel))
    assert not isinstance(klass, str)
    assert klass.name == name


def test_obs_store_and_unknown_files_are_violations() -> None:
    assert classify(Path("obs/deadbeef.txt")) == FORBIDDEN
    assert classify(Path("mystery.json")) == UNDECLARED


# ------------------------------------------------------------------------- audit


def test_fresh_daemon_state_dir_passes(daemon: Daemon) -> None:
    assert audit_state_dir(daemon.state_dir) == []


def test_clean_counts_log_passes(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    entry = {"event": "PostToolUse", "bytes_in": 12, "session_id": "s1", "outcome": "handled"}
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    assert audit_state_dir(tmp_path) == []


def test_counts_log_with_embedded_text_fails(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps({"event": "x", "pad": "line one\nline two"}) + "\n", encoding="utf-8"
    )
    assert audit_state_dir(tmp_path) == ["events.jsonl: multiline-text"]


def test_counts_log_with_a_long_string_fails(tmp_path: Path) -> None:
    path = tmp_path / "briefs.jsonl"
    path.write_text(json.dumps({"event": "x", "pad": "a" * 300}) + "\n", encoding="utf-8")
    assert audit_state_dir(tmp_path) == ["briefs.jsonl: long-text"]


def test_bounded_snapshot_prose_passes(tmp_path: Path) -> None:
    snapshot = tmp_path / "carry" / "s1.json"
    snapshot.parent.mkdir()
    snapshot.write_text(
        json.dumps(
            {
                "files": [{"path": "src/" + "nested/" * 20 + "app.py", "edited": True}],
                "todos": [{"s": "pending", "t": "finish the parser - " + "x" * 100}],
                "constraints": ["keep the public API stable"],
                "commands": ["git commit -m 'multi\nline message'"],
            }
        ),
        encoding="utf-8",
    )
    assert audit_state_dir(tmp_path) == []


def test_snapshot_with_a_text_blob_fails(tmp_path: Path) -> None:
    snapshot = tmp_path / "carry" / "s1.json"
    snapshot.parent.mkdir()
    snapshot.write_text(json.dumps({"files": [], "blob": "x" * 5_000}), encoding="utf-8")
    assert audit_state_dir(tmp_path) == ["carry/s1.json: long-text"]


def test_brief_packet_with_a_text_blob_fails(tmp_path: Path) -> None:
    packet = tmp_path / "briefs" / "ab12" / "tu-1.json"
    packet.parent.mkdir(parents=True)
    packet.write_text(json.dumps({"note": "x" * 20_000}), encoding="utf-8")
    assert audit_state_dir(tmp_path) == ["briefs/ab12/tu-1.json: oversized"]


def test_obs_store_and_undeclared_file_fail(tmp_path: Path) -> None:
    (tmp_path / "obs").mkdir()
    (tmp_path / "obs" / "deadbeef.txt").write_text("full tool output", encoding="utf-8")
    (tmp_path / "mystery.json").write_text("{}", encoding="utf-8")
    assert audit_state_dir(tmp_path) == [
        "mystery.json: undeclared",
        "obs/deadbeef.txt: forbidden (retired obs blob store)",
    ]


def test_atomic_write_temp_files_are_ignored(tmp_path: Path) -> None:
    (tmp_path / ".state.abc.tmp").write_text("x" * 10_000, encoding="utf-8")
    assert audit_state_dir(tmp_path) == []


# ---------------------------------------------------------------------- startup


def test_daemon_binds_loopback_only(daemon: Daemon) -> None:
    assert daemon._httpd is not None
    assert daemon._httpd.server_address[0] == "127.0.0.1"
    require_loopback_bind("127.0.0.1")
    with pytest.raises(RuntimeError, match="127.0.0.1 only"):
        require_loopback_bind("0.0.0.0")


def test_startup_deletes_a_legacy_obs_store(tmp_path: Path) -> None:
    state = tmp_path / "cfx"
    obs = state / "obs"
    obs.mkdir(parents=True)
    (obs / "deadbeef.txt").write_text("full tool output", encoding="utf-8")
    d = Daemon(state, 0)
    d.start()
    try:
        assert not obs.exists()
        assert (state / OBS_PURGE_FILE).read_text(encoding="utf-8")
        assert audit_state_dir(state) == []
        events = (state / "events.jsonl").read_text(encoding="utf-8")
        assert '"event": "obs_purged"' in events and "deadbeef" not in events
    finally:
        d.stop()


def test_startup_warns_when_a_state_file_exceeds_its_class(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "cfx"
    state.mkdir()
    planted = "x" * 500
    (state / "events.jsonl").write_text(json.dumps({"event": planted}) + "\n", encoding="utf-8")
    d = Daemon(state, 0)
    d.start()
    try:
        err = capsys.readouterr().err
        assert "1 state file(s) exceed their content class" in err
        assert planted not in err
    finally:
        d.stop()


@pytest.mark.skipif(not POSIX, reason="POSIX permissions")
def test_token_permission_warning_on_current_user_only(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("a" * 64, encoding="utf-8")
    os.chmod(token, 0o600)
    assert token_permission_warning(token) is None
    os.chmod(token, 0o644)
    warning = token_permission_warning(token)
    assert warning is not None and "readable by other users" in warning
    assert stat.S_IMODE(token.stat().st_mode) == 0o644


@pytest.mark.skipif(not POSIX, reason="POSIX permissions")
def test_startup_warns_on_a_loose_token(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "cfx"
    state.mkdir()
    (state / "token").write_text("a" * 64, encoding="utf-8")
    os.chmod(state / "token", 0o644)
    d = Daemon(state, 0)
    d.start()
    try:
        assert "readable by other users" in capsys.readouterr().err
    finally:
        d.stop()


class _Proc:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.returncode = 0


def _icacls(output: str) -> Any:
    return lambda *args, **kwargs: _Proc(output)


def test_windows_acl_warning_on_a_broad_group(tmp_path: Path) -> None:
    token = tmp_path / "token"
    output = f"{token} BUILTIN\\Users:(R)\n\nSuccessfully processed 1 files;\n"
    warning = token_permission_warning(token, platform_name="nt", runner=_icacls(output))
    assert warning is not None and "'users'" in warning


def test_windows_acl_passes_for_owner_system_and_administrators(tmp_path: Path) -> None:
    token = tmp_path / "token"
    output = (
        f"{token} NT AUTHORITY\\SYSTEM:(F)\n"
        f"               BUILTIN\\Administrators:(F)\n"
        f"               MACHINE\\prana:(F)\n"
    )
    assert token_permission_warning(token, platform_name="nt", runner=_icacls(output)) is None


def test_windows_acl_unverifiable_warns(tmp_path: Path) -> None:
    token = tmp_path / "token"

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("icacls missing")

    warning = token_permission_warning(token, platform_name="nt", runner=boom)
    assert warning is not None and "could not verify" in warning


# ------------------------------------------------------------------ docs numbers


def _carry_bytes_doc(tmp_path: Path, v2: int, v1: int) -> None:
    (tmp_path / "SECURITY.md").write_text(
        f"Carry re-injects up to {v2:,} bytes (v1: {v1:,}) and is off by default.\n",
        encoding="utf-8",
    )


def test_security_carry_bytes_match_the_code(tmp_path: Path) -> None:
    _carry_bytes_doc(tmp_path, carry_mod.CARRY_V2_CAP_BYTES, carry_mod.RENDER_CAP_BYTES)
    report: list[tuple[bool, str]] = []
    DOCS.check_security_carry_bytes(tmp_path, report)
    assert [ok for ok, _ in report] == [True]


def test_security_carry_bytes_drift_fails(tmp_path: Path) -> None:
    _carry_bytes_doc(tmp_path, carry_mod.CARRY_V2_CAP_BYTES + 1, carry_mod.RENDER_CAP_BYTES)
    report: list[tuple[bool, str]] = []
    DOCS.check_security_carry_bytes(tmp_path, report)
    assert [ok for ok, _ in report] == [False]


def test_security_carry_bytes_figure_missing_fails(tmp_path: Path) -> None:
    (tmp_path / "SECURITY.md").write_text("Carry is bounded.\n", encoding="utf-8")
    report: list[tuple[bool, str]] = []
    DOCS.check_security_carry_bytes(tmp_path, report)
    assert [ok for ok, _ in report] == [False]


def test_real_docs_numbers_pass() -> None:
    assert [message for ok, message in DOCS.run() if not ok] == []
