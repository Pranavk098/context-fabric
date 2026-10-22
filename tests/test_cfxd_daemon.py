"""cfxd daemon tests: security model, handlers, fail-open, event log, latency.

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


def _post(
    port: int,
    event: str,
    payload: bytes,
    *,
    headers: dict[str, str] | None = None,
    host: str | None = None,
) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        if host is not None:
            conn.putrequest("POST", f"/hooks/{event}", skip_host=True)
            conn.putheader("Host", host)
            for key, value in h.items():
                conn.putheader(key, value)
            conn.putheader("Content-Length", str(len(payload)))
            conn.endheaders()
            conn.send(payload)
        else:
            conn.request("POST", f"/hooks/{event}", body=payload, headers=h)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, json.loads(body) if body else {}
    finally:
        conn.close()


def _get_health(port: int, *, host: str | None = None) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        if host is not None:
            conn.putrequest("GET", "/health", skip_host=True)
            conn.putheader("Host", host)
            conn.endheaders()
        else:
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


def test_health_bad_host_rejected(daemon: Daemon) -> None:
    status, _ = _get_health(daemon.port, host="evil.example.com")
    assert status == 403


def test_normal_hook_post_works(daemon: Daemon) -> None:
    status, body = _post(daemon.port, "PreToolUse", b"{}")
    assert status == 200
    assert body == {}


def test_origin_header_rejected(daemon: Daemon) -> None:
    status, body = _post(
        daemon.port, "PreToolUse", b"{}", headers={"Origin": "https://evil.example.com"}
    )
    assert status == 403
    assert body == {"error": "forbidden"}


def test_bad_host_rejected(daemon: Daemon) -> None:
    status, body = _post(daemon.port, "PreToolUse", b"{}", host="evil.example.com:9999")
    assert status == 403
    assert body == {"error": "forbidden"}


def test_non_json_content_type_rejected(daemon: Daemon) -> None:
    status, _ = _post(daemon.port, "PreToolUse", b"{}", headers={"Content-Type": "text/plain"})
    assert status == 403


def test_json_content_type_with_charset_accepted(daemon: Daemon) -> None:
    status, _ = _post(
        daemon.port,
        "PreToolUse",
        b"{}",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert status == 200


def test_rejection_logs_outcome_rejected(daemon: Daemon) -> None:
    _post(daemon.port, "PreToolUse", b"{}", headers={"Origin": "https://evil.example.com"})
    events = _read_events(daemon)
    assert any(e["outcome"] == "rejected" for e in events)


def test_unknown_event_returns_empty(daemon: Daemon) -> None:
    status, body = _post(daemon.port, "NoSuchEvent", b"{}")
    assert status == 200
    assert body == {}


def test_registered_echo_handler_returns_output(daemon: Daemon) -> None:
    register("EchoEvent", echo)
    status, body = _post(daemon.port, "EchoEvent", b'{"hello": "world"}')
    assert status == 200
    assert body == {"hello": "world"}


def test_raising_handler_returns_empty_and_logs_error(daemon: Daemon) -> None:
    def boom(payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("boom")

    register("BoomEvent", boom)
    status, body = _post(daemon.port, "BoomEvent", b'{"x": 1}')
    assert status == 200
    assert body == {}
    assert any(e["event"] == "BoomEvent" and e["outcome"] == "error" for e in _read_events(daemon))


def test_slow_handler_returns_empty(daemon: Daemon) -> None:
    def slow(payload: dict[str, Any]) -> dict[str, Any]:
        time.sleep(2.0)
        return {"slow": True}

    register("SlowEvent", slow)
    t0 = time.perf_counter()
    status, body = _post(daemon.port, "SlowEvent", b'{"x": 1}')
    elapsed = time.perf_counter() - t0
    assert status == 200
    assert body == {}
    # The deadline must cut the hook off, not wait for the handler to finish.
    assert elapsed < 0.5


def test_events_log_has_no_payload_content(daemon: Daemon) -> None:
    secret = "sekrit-MARKER-9f8e7d6c5b4a3210"
    payload = json.dumps({"tool_name": "Bash", "session_id": "sess-1", "content": secret}).encode()
    _post(daemon.port, "PreToolUse", payload)
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
                headers={"Content-Type": "application/json"},
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


def test_second_daemon_cannot_bind_same_port(daemon: Daemon, tmp_path: Path) -> None:
    # Two sessions starting together must not end up with two daemons on one port.
    second = Daemon(tmp_path / "other", daemon.port)
    with pytest.raises(OSError):
        second.start()
