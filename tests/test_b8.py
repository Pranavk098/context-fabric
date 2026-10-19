"""B8 unit tests — model allow-list, daily-cap stop/resume, window-cap error shape,
sampler determinism, and resume skip. No network, no GPU, no Docker."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

from evals.b8_live import (
    finished_pairs,
    pending_pairs,
    sample_instances,
)
from sidecar import b8
from sidecar.server import make_server

_DAY = "2026-10-19"


# --------------------------------------------------------------------- fake upstream


class _EchoUpstream(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.dumps(
            {
                "id": "cmpl-test",
                "model": json.loads(body).get("model", ""),
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 1,
                    "total_tokens": 8,
                    "prompt_tokens_details": {"cached_tokens": 3},
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def _serve(server: ThreadingHTTPServer) -> str:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


def _start_upstream() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoUpstream)
    return server, _serve(server)


def _stop(*servers: ThreadingHTTPServer) -> None:
    for server in servers:
        server.shutdown()
        server.server_close()


def _post(url: str, payload: dict[str, Any]) -> httpx.Response:
    return httpx.post(f"{url}/v1/chat/completions", json=payload, timeout=10.0)


def _messages(text: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": text}]


# -------------------------------------------------------------------- model allow-list


def test_allow_list_rejects_disallowed_model(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream()
    ledger = b8.Ledger(tmp_path / "ledger")
    sidecar = make_server(upstream=upstream_url, mode="passthrough", port=0, b8_ledger=ledger)
    sidecar_url = _serve(sidecar)
    try:
        rejected = _post(sidecar_url, {"model": "gpt-4o", "messages": _messages("hi")})
        assert rejected.status_code == 400
        assert rejected.json()["error"]["code"] == "model_not_allowed"
        # provider-prefixed name is still allowed
        ok = _post(sidecar_url, {"model": "openai/gpt-4.1-mini", "messages": _messages("hi")})
        assert ok.status_code == 200
    finally:
        _stop(sidecar, upstream)


def test_allow_list_off_forwards_disallowed_model(tmp_path: Path) -> None:
    """Without a ledger the sidecar is byte-identical: no model gate, no parsing."""
    upstream, upstream_url = _start_upstream()
    sidecar = make_server(upstream=upstream_url, mode="passthrough", port=0)
    sidecar_url = _serve(sidecar)
    try:
        response = _post(sidecar_url, {"model": "gpt-4o", "messages": _messages("hi")})
        assert response.status_code == 200
    finally:
        _stop(sidecar, upstream)


# ------------------------------------------------------------------- daily cap


def test_ledger_sums_per_day_and_resets_next_day(tmp_path: Path) -> None:
    ledger = b8.Ledger(tmp_path / "ledger")
    ledger.append(_DAY, model="gpt-4.1-mini", prompt_tokens=100, completion_tokens=20)
    ledger.append(
        _DAY, model="gpt-4.1-mini", prompt_tokens=30, completion_tokens=5, cached_tokens=10
    )
    summary = ledger.summary(_DAY)
    assert summary["total_tokens"] == 155
    assert summary["prompt_tokens"] == 130
    assert summary["completion_tokens"] == 25
    assert summary["cached_tokens"] == 10
    assert summary["uncached_tokens"] == 120
    # a fresh day (the "resume tomorrow" state) starts from zero
    assert ledger.day_total("2026-10-20") == 0
    assert ledger.summary("2026-10-20")["total_tokens"] == 0


def test_daily_cap_stop_returns_resume_tomorrow(tmp_path: Path) -> None:
    ledger = b8.Ledger(tmp_path / "ledger")
    day = b8.utc_day()
    ledger.append(day, model="gpt-4.1-mini", prompt_tokens=48, completion_tokens=2)
    upstream, upstream_url = _start_upstream()
    sidecar = make_server(
        upstream=upstream_url,
        mode="passthrough",
        port=0,
        b8_ledger=ledger,
        b8_daily_cap=50,
    )
    sidecar_url = _serve(sidecar)
    try:
        response = _post(sidecar_url, {"model": "gpt-4.1-mini", "messages": _messages("hi")})
        assert response.status_code == 429
        error = response.json()["error"]
        assert error["code"] == "daily_token_cap_exceeded"
        assert "Resume tomorrow" in error["message"]
    finally:
        _stop(sidecar, upstream)


def test_daily_cap_records_usage_after_proxy(tmp_path: Path) -> None:
    ledger = b8.Ledger(tmp_path / "ledger")
    upstream, upstream_url = _start_upstream()
    sidecar = make_server(
        upstream=upstream_url,
        mode="passthrough",
        port=0,
        b8_ledger=ledger,
        b8_daily_cap=1_000_000,
    )
    sidecar_url = _serve(sidecar)
    try:
        response = _post(sidecar_url, {"model": "gpt-4.1-mini", "messages": _messages("hi")})
        assert response.status_code == 200
    finally:
        _stop(sidecar, upstream)
    summary = ledger.summary(b8.utc_day())
    assert summary["total_tokens"] == 8  # 7 prompt + 1 completion from the fake upstream


# -------------------------------------------------------------------- window cap


def test_window_cap_error_shape(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream()
    sidecar = make_server(upstream=upstream_url, mode="passthrough", port=0, window_cap=20)
    sidecar_url = _serve(sidecar)
    try:
        long_text = "word " * 200
        response = _post(sidecar_url, {"model": "gpt-4.1-mini", "messages": _messages(long_text)})
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "context_length_exceeded"
        assert error["type"] == "invalid_request_error"
        assert error["param"] == "messages"
        assert "maximum context length is 20 tokens" in error["message"]
        assert "in the messages" in error["message"]
    finally:
        _stop(sidecar, upstream)


def test_window_cap_off_forwards_long_body(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream()
    sidecar = make_server(upstream=upstream_url, mode="passthrough", port=0)
    sidecar_url = _serve(sidecar)
    try:
        response = _post(
            sidecar_url, {"model": "gpt-4.1-mini", "messages": _messages("word " * 200)}
        )
        assert response.status_code == 200
    finally:
        _stop(sidecar, upstream)


# ----------------------------------------------------------------- sampler determinism


def _instances() -> list[dict[str, Any]]:
    instances: list[dict[str, Any]] = []
    for repo, count in (("a/a", 10), ("b/b", 5), ("c/c", 2)):
        for i in range(count):
            instances.append({"instance_id": f"{repo.replace('/', '_')}__{i}", "repo": repo})
    return instances


def test_sampler_is_deterministic() -> None:
    instances = _instances()
    assert sample_instances(instances, 20261019, 30) == sample_instances(instances, 20261019, 30)
    assert sample_instances(instances, 7, 6) == sample_instances(instances, 7, 6)


def test_sampler_floor_covers_every_repo() -> None:
    instances = _instances()
    result = sample_instances(instances, 20261019, 5)
    assert len(result) == 5
    assert {i["repo"] for i in result} == {"a/a", "b/b", "c/c"}
    # output is sorted by instance_id and a subset of the input
    ids = [i["instance_id"] for i in result]
    assert ids == sorted(ids)
    assert set(ids) <= {i["instance_id"] for i in instances}


def test_sampler_respects_n() -> None:
    instances = _instances()
    assert len(sample_instances(instances, 1, 3)) == 3
    assert len(sample_instances(instances, 1, 0)) == 0
    assert sample_instances([], 1, 5) == []


# ----------------------------------------------------------------------- resume skip


def test_resume_skip_excludes_finished_pairs(tmp_path: Path) -> None:
    predictions = tmp_path / "preds.jsonl"
    predictions.write_text(
        json.dumps(
            {
                "instance_id": "a_a__0",
                "model_name_or_path": "gpt-4.1-mini",
                "model_patch": "diff",
                "arm": "A",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    done = finished_pairs(predictions)
    assert done == {("a_a__0", "A")}
    work = pending_pairs(_instances(), ("A", "B"), done)
    assert ("a_a__0", "A") not in [(i["instance_id"], a) for i, a in work]
    assert ("a_a__0", "B") in [(i["instance_id"], a) for i, a in work]


def test_finished_pairs_missing_file_is_empty(tmp_path: Path) -> None:
    assert finished_pairs(tmp_path / "nope.jsonl") == set()
