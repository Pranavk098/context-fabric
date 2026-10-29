"""cfxd security hardening tests: POSIX perms, HTTP limits, redaction, sanitisation, carry.

These cover the fixes that don't need a subprocess: directory/log modes (A), the
pre-auth request limits and log rotation (B), the secret-redaction matrix (C),
the Unicode sanitisation (D), and the carry snapshot guards (G).
"""

from __future__ import annotations

import http.client
import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import hook_headers

import cfxd.daemon as daemon_mod
import cfxd.fold as fold_mod
from cfxd import session_state as ss
from cfxd.brief import _sanitize, handle_pre_tool_use
from cfxd.carry import handle_pre_compact, handle_session_start
from cfxd.daemon import Daemon
from cfxd.fold import handle_post_tool_use
from cfxd.handlers import clear

POSIX = os.name != "nt"


@pytest.fixture(autouse=True)
def _clean() -> Any:
    ss.reset()
    clear()
    yield
    ss.reset()
    clear()


@pytest.fixture
def daemon(tmp_path: Path) -> Iterator[Daemon]:
    d = Daemon(tmp_path / "cfx", 0)
    d.start()
    try:
        yield d
    finally:
        d.stop()


def _raw_request(
    port: int, headers: dict[str, str], *, body: bytes = b"", path: str = "/hooks/PreToolUse"
) -> tuple[int, dict[str, Any]]:
    """Send a POST with explicit headers (and optionally no/short body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("POST", path, skip_host=True)
        conn.putheader("Host", f"127.0.0.1:{port}")
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders()
        if body:
            conn.send(body)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, json.loads(data) if data else {}
    finally:
        conn.close()


def _seed_file(path: str, *, lines: int | None = 42) -> None:
    ss.observe_post_tool_use(
        {
            "session_id": "s1",
            "tool_name": "Read",
            "tool_input": {"file_path": path},
            "tool_response": {"file": {"numLines": lines, "totalLines": lines}},
        }
    )


# --------------------------------------------------------------------------- A


@pytest.mark.skipif(not POSIX, reason="POSIX permissions")
def test_state_dir_token_and_events_restrictive(daemon: Daemon) -> None:
    state = daemon.state_dir
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "token").stat().st_mode) == 0o600
    daemon.log_event({"ts": "x", "event": "e"})
    assert stat.S_IMODE((state / "events.jsonl").stat().st_mode) == 0o600


@pytest.mark.skipif(not POSIX, reason="POSIX permissions")
def test_obs_and_folds_restrictive(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    from cfxd.config import set_flag

    set_flag(state_dir, "fold", True)
    set_flag(state_dir, "fold_read", True)
    content = "def a():\n" + ("# pad\n" * 20_000)
    result = handle_post_tool_use(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "mod.py"},
            "tool_response": {
                "type": "text",
                "file": {"content": content, "numLines": 2, "totalLines": 2},
            },
            "session_id": "s1",
        },
        state_dir,
    )
    assert result
    assert stat.S_IMODE((state_dir / "obs").stat().st_mode) == 0o700
    assert stat.S_IMODE((state_dir / "folds.jsonl").stat().st_mode) == 0o600


@pytest.mark.skipif(not POSIX, reason="POSIX permissions")
def test_carry_and_brief_logs_restrictive(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    _seed_file("/a/auth.py")
    handle_pre_compact({"session_id": "s1", "trigger": "auto"}, state_dir)
    assert stat.S_IMODE((state_dir / "carry").stat().st_mode) == 0o700
    assert stat.S_IMODE((state_dir / "carry.jsonl").stat().st_mode) == 0o600
    handle_pre_tool_use(
        {"tool_name": "Agent", "session_id": "s1", "tool_input": {"prompt": "P" * 300}},
        state_dir,
    )
    assert stat.S_IMODE((state_dir / "briefs.jsonl").stat().st_mode) == 0o600


# --------------------------------------------------------------------------- B


def test_content_length_over_cap_413_no_read(daemon: Daemon) -> None:
    status, body = _raw_request(
        daemon.port,
        hook_headers(daemon),
        path="/hooks/PreToolUse",
    )
    assert status == 200  # empty body is fine
    # A declared (but unsent) gigantic body must be rejected without a read.
    conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=5)
    try:
        conn.putrequest("POST", "/hooks/PreToolUse", skip_host=True)
        conn.putheader("Host", f"127.0.0.1:{daemon.port}")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(9 * 1024 * 1024))
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 413
        assert json.loads(resp.read()) == {"error": "too large"}
    finally:
        conn.close()


def test_malformed_content_length_400(daemon: Daemon) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=5)
    try:
        conn.putrequest("POST", "/hooks/PreToolUse", skip_host=True)
        conn.putheader("Host", f"127.0.0.1:{daemon.port}")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "abc")
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 400
    finally:
        conn.close()


def test_negative_content_length_400(daemon: Daemon) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=5)
    try:
        conn.putrequest("POST", "/hooks/PreToolUse", skip_host=True)
        conn.putheader("Host", f"127.0.0.1:{daemon.port}")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "-1")
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 400
    finally:
        conn.close()


def test_rejection_happens_before_body_read(daemon: Daemon) -> None:
    # A bad Host must be rejected with the (large) body unread and unlogged.
    conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=5)
    try:
        conn.putrequest("POST", "/hooks/PreToolUse", skip_host=True)
        conn.putheader("Host", "evil.example.com:9999")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(9 * 1024 * 1024))
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 403
    finally:
        conn.close()
    assert not daemon.events_path.exists()


def test_concurrent_connection_cap(tmp_path: Path) -> None:
    d = Daemon(tmp_path / "cfx", 0)
    d.start()
    try:
        server = d._httpd
        assert server is not None
        # Simulate the in-flight counter sitting at the cap; the next accepted
        # connection must be rejected immediately with 503 (no handler runs).
        with server._conn_lock:
            server._in_flight = daemon_mod.MAX_CONNECTIONS
        conn = http.client.HTTPConnection("127.0.0.1", d.port, timeout=5)
        try:
            conn.request(
                "POST",
                "/hooks/PreToolUse",
                body=b"{}",
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            assert resp.status == 503
        finally:
            conn.close()
    finally:
        d.stop()


def test_events_log_rotates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon_mod, "EVENTS_ROTATE_BYTES", 200)
    d = Daemon(tmp_path / "cfx", 0)
    d.start()
    try:
        for i in range(20):
            d.log_event({"ts": "x", "event": "e", "n": i, "pad": "x" * 50})
    finally:
        d.stop()
    assert (tmp_path / "cfx" / "events.jsonl.1").exists()
    assert (tmp_path / "cfx" / "events.jsonl").exists()


def test_folds_log_rotates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fold_mod, "FOLDS_ROTATE_BYTES", 200)
    state_dir = tmp_path / "cfx"
    state_dir.mkdir()
    for i in range(20):
        fold_mod._log_fold(state_dir, "s1", "Bash", "stdout", "other", 1000, 100, f"obs{i:016x}")
    assert (state_dir / "folds.jsonl.1").exists()
    assert (state_dir / "folds.jsonl").exists()


# --------------------------------------------------------------------------- C

SECRET_CASES: list[tuple[str, str, str]] = [
    ('export TOKEN="my secret value"', "my secret value", "TOKEN=***"),
    ("export TOKEN='my secret value'", "my secret value", "TOKEN=***"),
    ('AWS_SECRET_ACCESS_KEY="wJalrXUtnFEMI"', "wJalrXUtnFEMI", "AWS_SECRET_ACCESS_KEY=***"),
    ("API_KEY=sekrit go", "sekrit", "API_KEY=***"),
    ("export TOKEN=sekrit", "sekrit", "TOKEN=***"),
    ("mysql --password foo", "foo", "--password ***"),
    ("mysql --password=foo", "foo", "--password=***"),
    ("mysql --pass bar", "bar", "--pass ***"),
    ("mysql --passwd=baz", "baz", "--passwd=***"),
    ("curl --token abc", "abc", "--token ***"),
    ("curl --secret s3cret", "s3cret", "--secret ***"),
    ("curl --api-key keyvalue123", "keyvalue123", "--api-key ***"),
    ("mysql -p sekrit", "sekrit", "-p ***"),
    ("sshpass -p hunter2", "hunter2", "-p ***"),
    ("mysqldump -psekrit db", "sekrit", "-p***"),
    ("curl -u user:pass", "user:pass", "-u user:***"),
    ("curl --user user:pass", "user:pass", "--user user:***"),
    ('curl -H "Authorization: Basic dXNlcjpwYXNz"', "dXNlcjpwYXNz", "Authorization: ***"),
    ("curl -H 'X-Api-Key: abc123'", "abc123", "X-Api-Key: ***"),
    ("curl -H 'Cookie: session=abc'", "session=abc", "Cookie: ***"),
    ('curl -H "Set-Cookie: sid=123"', "sid=123", "Set-Cookie: ***"),
    ('curl -H "Proxy-Authorization: Bearer xyz789"', "xyz789", "Bearer ***"),
    ("git clone https://user:hunter2@example.com/repo.git", "hunter2", "user:***@"),
    ("git clone https://ghp_TOKEN@github.com/foo", "ghp_TOKEN", "***@github.com"),
    ("npm config set //registry.npmjs.org/:_authToken=npm_xyz", "npm_xyz", "_authToken=***"),
    ("curl -H 'X-Auth-Token: s3cr3t' u", "s3cr3t", "X-Auth-Token: ***"),
    ("curl -H 'X-Session-Key: s3cr3t' u", "s3cr3t", "X-Session-Key: ***"),
    ("aws configure set aws_secret_access_key s3cr3t", "s3cr3t", "aws_secret_access_key ***"),
    ("docker login -u me -p s3cr3t", "s3cr3t", "-p ***"),
    ("docker login -u me --password s3cr3t", "s3cr3t", "--password ***"),
    ("npm config set //r.org/:_authToken s3cr3t", "s3cr3t", "_authToken ***"),
    ('curl -d \'{"password":"s3cr3t"}\' u', "s3cr3t", '"password":"***"'),
    ('curl -d \'{"api_key": "s3cr3t"}\' u', "s3cr3t", '"api_key": "***"'),
    ("psql -W s3cr3t db", "s3cr3t", "-W ***"),
    ("openssl enc -k s3cr3t", "s3cr3t", "-k ***"),
    ("openssl enc -pass pass:s3cr3t", "s3cr3t", "pass:***"),
    ("curl -H 'Authorization:Bearer\\ s3cr3t' u", "s3cr3t", "***"),
    ("curl -H 'Authorization:Bearers3cr3t' u", "s3cr3t", "***"),
    ("echo glpat-abcdefghijklmnopqrst", "glpat-abcdefghijklmnopqrst", "***"),
    (
        "echo hf_abcdefghijklmnopqrstuvwxyz0123456789",
        "hf_abcdefghijklmnopqrstuvwxyz0123456789",
        "***",
    ),
    ("echo npm_abcdefghijklmnopqrstuvwx", "npm_abcdefghijklmnopqrstuvwx", "***"),
    ("echo sk_live_abcdefghijklmnop", "sk_live_abcdefghijklmnop", "***"),
    ("echo pypi-AgEIcHlwaS5vcmcabcdefghijklmnop", "pypi-AgEIcHlwaS5vcmcabcdefghijklmnop", "***"),
    ("echo ghp_1234567890abcdef", "ghp_1234567890abcdef", "***"),
    ("echo gho_1234567890abcdef", "gho_1234567890abcdef", "***"),
    ("echo github_pat_11ABCDEFG", "github_pat_11ABCDEFG", "***"),
    ("echo sk-ant-api03-abcdefghijklmnop", "sk-ant-api03-abcdefghijklmnop", "***"),
    ("echo xoxb-1234567890-abcdef", "xoxb-1234567890-abcdef", "***"),
    ("echo AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE", "***"),
    (
        "echo eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig",
        "eyJhbGciOiJIUzI1NiJ9",
        "***",
    ),
]

BENIGN_CASES = [
    "ls -pa",
    "serve --port 8080",
    "psql -p 5432",
    "ssh -p 2222 host",
    "git push -u origin main",
    "docker run -p 8080:80 image",
    "curl -X POST https://api.example.com/data",
    'echo "PORT=8080"',
    "git clone https://github.com/user/repo.git",
    'python -c "print(1)"',
    "npm install --save-dev express",
    "ls -la",
    'echo "short sk-abcd"',
    "export FOO=bar",
    "mysql -p",
    "git log --oneline",
    "pytest -k tokenizer",
    "ls hf_models_dir",
]


@pytest.mark.parametrize("cmd,secret,marker", SECRET_CASES)
def test_redact_secret_cases(cmd: str, secret: str, marker: str) -> None:
    out = ss.redact_command(cmd)
    assert secret not in out, (cmd, out)
    assert marker in out, (cmd, out)


@pytest.mark.parametrize("cmd", BENIGN_CASES)
def test_redact_benign_cases(cmd: str) -> None:
    assert ss.redact_command(cmd) == cmd


def test_redact_url_multi_at_password() -> None:
    out = ss.redact_command("psql 'postgresql://u:p@ss@db/x'")
    assert "ss@db" not in out, out
    assert "u:***@db" in out, out


def test_parse_todos_redacts_secrets() -> None:
    parsed = ss._parse_todos([{"status": "pending", "content": "export API_KEY=s3cr3t"}])
    assert parsed == [("pending", "export API_KEY=***")]


def test_fold_source_webfetch_drops_userinfo_and_deep_path() -> None:
    src = fold_mod._fold_source("WebFetch", {"url": "https://u:pw@h.com/hooks/T0/B0/SECRET?x=1"})
    assert src == "https://h.com/hooks/T0", src
    assert "pw" not in src
    assert "SECRET" not in src


def test_fold_source_read_path_unchanged() -> None:
    assert fold_mod._fold_source("Read", {"file_path": "/a/b.py"}) == "/a/b.py"


# --------------------------------------------------------------------------- D


def test_sanitize_drops_unicode_controls_and_separators() -> None:
    assert _sanitize("a\u2028b\u0085c") == "abc"  # line/next-line separators
    assert _sanitize("a\u200e\u200f\u202a b") == "a b"  # bidi marks (Cf)
    assert _sanitize("a\u00a0b") == "ab"  # nbsp (Zs) is not plain space
    assert _sanitize("plain text  here") == "plain text  here"  # normal space kept
    assert _sanitize("x`<>y") == "xy"  # existing delimiter chars still dropped


# --------------------------------------------------------------------------- G


def _snap(fold_id: str) -> dict[str, Any]:
    return {
        "v": 0,
        "sid": "s1",
        "files": [],
        "folds": [{"id": fold_id, "tool": "Bash", "src": "cmd", "bytes": 100, "kind": "test"}],
        "commands": [],
        "todos": [],
    }


def test_carry_rejects_non_hex_fold_id(tmp_path: Path) -> None:
    from cfxd.carry import _snapshot_to_facts

    state_dir = tmp_path / "cfx"
    assert _snapshot_to_facts(_snap("deadbeef00000000"), state_dir) is not None
    assert _snapshot_to_facts(_snap("../../evil"), state_dir) is None
    assert _snapshot_to_facts(_snap("NOT-HEX!!"), state_dir) is None
    assert _snapshot_to_facts(_snap("toolong00000000000000000"), state_dir) is None


def test_carry_rejects_oversized_snapshot_file(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    carry_dir = state_dir / "carry"
    carry_dir.mkdir(parents=True)
    (carry_dir / "s1.json").write_text("x" * (64 * 1024 + 1), encoding="utf-8")
    result = handle_session_start({"session_id": "s1", "source": "compact"}, state_dir)
    assert result == {}
