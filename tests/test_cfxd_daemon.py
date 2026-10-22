"""cfxd daemon tests: auth, handlers, fail-open, event log, latency, single-instance.

The daemon runs in-process in a background thread with a fresh state dir per
test, so handler registration and event-log assertions see the real process.
"""

from __future__ import annotations

import http.client
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cfxd.daemon import Daemon, percentile, run_serve
from cfxd.handlers import clear, echo, register


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


def _post(port: int, token: str | None, event: str, payload: bytes) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        conn.request("POST", f"/hooks/{event}", body=payload, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, json.loads(body) if body else {}
    finally:
        conn.close()


def _get_health(port: int) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        conn.request("GET", "/health")
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


def _read_events(daemon: Daemon) -> list[dict[str, Any]]:
    if not daemon.events_path.exists():
        return []
    text = daemon.events_path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_health_ok(daemon: Daemon) -> None:
    status, body = _get_health(daemon.port)
    assert status == 200
    assert body["ok"] is True
    assert body["pid"] == os.getpid()
    assert body["version"]


def test_missing_token_returns_401(daemon: Daemon) -> None:
    status, _ = _post(daemon.port, None, "PreToolUse", b"{}")
    assert status == 401


def test_wrong_token_returns_401(daemon: Daemon) -> None:
    status, _ = _post(daemon.port, "not-the-token", "PreToolUse", b"{}")
    assert status == 401


def test_unknown_event_returns_empty(daemon: Daemon) -> None:
    status, body = _post(daemon.port, daemon.token, "NoSuchEvent", b"{}")
    assert status == 200
    assert body == {}


def test_registered_echo_handler_returns_output(daemon: Daemon) -> None:
    register("EchoEvent", echo)
    status, body = _post(daemon.port, daemon.token, "EchoEvent", b'{"hello": "world"}')
    assert status == 200
    assert body == {"hello": "world"}


def test_raising_handler_returns_empty_and_logs_error(daemon: Daemon) -> None:
    def boom(payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("boom")

    register("BoomEvent", boom)
    status, body = _post(daemon.port, daemon.token, "BoomEvent", b'{"x": 1}')
    assert status == 200
    assert body == {}
    assert any(e["event"] == "BoomEvent" and e["outcome"] == "error" for e in _read_events(daemon))


def test_slow_handler_returns_empty(daemon: Daemon) -> None:
    def slow(payload: dict[str, Any]) -> dict[str, Any]:
        time.sleep(2.0)
        return {"slow": True}

    register("SlowEvent", slow)
    t0 = time.perf_counter()
    status, body = _post(daemon.port, daemon.token, "SlowEvent", b'{"x": 1}')
    elapsed = time.perf_counter() - t0
    assert status == 200
    assert body == {}
    # The deadline must cut the hook off, not wait for the handler to finish.
    assert elapsed < 0.5


def test_events_log_has_no_payload_content(daemon: Daemon) -> None:
    secret = "sekrit-MARKER-9f8e7d6c5b4a3210"
    payload = json.dumps({"tool_name": "Bash", "session_id": "sess-1", "content": secret}).encode()
    _post(daemon.port, daemon.token, "PreToolUse", payload)
    text = daemon.events_path.read_text(encoding="utf-8")
    assert secret not in text
    assert "9f8e7d6c5b4a3210" not in text


def test_p95_round_trip_under_50ms(daemon: Daemon) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=5)
    try:
        samples: list[float] = []
        for _ in range(200):
            start = time.perf_counter()
            conn.request(
                "POST",
                "/hooks/PreToolUse",
                body=b"{}",
                headers={
                    "Authorization": f"Bearer {daemon.token}",
                    "Content-Type": "application/json",
                },
            )
            resp = conn.getresponse()
            resp.read()
            samples.append((time.perf_counter() - start) * 1000.0)
    finally:
        conn.close()
    assert percentile(samples, 95) < 50.0


def test_second_serve_reports_already_running(daemon: Daemon, capsys: Any) -> None:
    assert run_serve(daemon.state_dir, 0) == 0
    assert "already running" in capsys.readouterr().out
