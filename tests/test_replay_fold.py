"""``cfx replay-fold`` — reconstruct folded requests byte-for-byte and expand losslessly."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

from kernel.store import SqliteStore
from sidecar.cli import main as cfx_main
from sidecar.obsfold import fold_messages
from sidecar.replay import fold_replay_bodies, replay_fold
from sidecar.server import make_server, open_store

AS_OF = datetime(2026, 1, 1, tzinfo=UTC)


def obs_messages(count: int) -> list[dict[str, Any]]:
    """A synthetic session: system + one user + ``count`` assistant/tool turns."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "you are a coding sub-agent"},
        {"role": "user", "content": "fix the bug"},
    ]
    for i in range(count):
        messages.append({"role": "assistant", "content": f"calling tool {i}"})
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{i}",
                "name": "read_file",
                "content": f"line one of file {i}\n" + " ".join(f"body{i}_{j}" for j in range(40)),
            }
        )
    return messages


def event_dicts(result: Any) -> list[dict[str, Any]]:
    return [
        {
            "message_index": event.message_index,
            "stub_id": event.stub_id,
            "content_hash": event.content_hash,
            "tool_name": event.tool_name,
            "content_bytes": event.content_bytes,
        }
        for event in result.events
    ]


def _recorded(
    tmp_path: Path, counts: tuple[int, ...]
) -> tuple[SqliteStore, list[list[dict[str, Any]]]]:
    """Fold each synthetic session and record it exactly as the sidecar does."""
    store = open_store(tmp_path / "obs.db")
    sent: list[list[dict[str, Any]]] = []
    for count in counts:
        messages = obs_messages(count)
        sent.append(messages)
        result = fold_messages(messages, n=3, k=3, store=store, session_id="s1", created_at=AS_OF)
        assert result.folded, "synthetic session must fold"
        store.log_fold_request(
            "s1", {"model": "m", "messages": messages}, event_dicts(result), result.seen_hash, AS_OF
        )
    return store, sent


def test_fold_request_store_roundtrip(tmp_path: Path) -> None:
    store, sent = _recorded(tmp_path, (6,))
    requests = store.load_fold_requests(session_id="s1")
    assert len(requests) == 1
    (request,) = requests
    assert request["session_id"] == "s1"
    assert request["request"] == {"model": "m", "messages": sent[0]}
    assert request["seen_hash"].startswith("sha256:")
    assert len(request["events"]) == 3  # 6 obs, n=3, k=3 folds the 3 oldest
    assert store.load_fold_requests(session_id="nobody") == []


def test_replay_fold_ok(tmp_path: Path, capsys: Any) -> None:
    store, _ = _recorded(tmp_path, (6, 7, 8, 9))
    assert replay_fold(tmp_path / "obs.db", "s1") == 0
    out = capsys.readouterr().out
    assert out.count("FOLD REPLAY OK") == 4


def test_replay_fold_empty_store_exits_1(tmp_path: Path, capsys: Any) -> None:
    open_store(tmp_path / "obs.db")
    assert replay_fold(tmp_path / "obs.db") == 1
    assert "no folded requests" in capsys.readouterr().err


def test_replay_fold_detects_seen_hash_tamper(tmp_path: Path, capsys: Any) -> None:
    store, _ = _recorded(tmp_path, (6,))
    conn = sqlite3.connect(tmp_path / "obs.db")
    conn.execute("UPDATE fold_requests SET seen_hash = 'sha256:bogus'")
    conn.commit()
    conn.close()
    assert replay_fold(tmp_path / "obs.db", "s1") == 1
    assert "seen hash mismatch" in capsys.readouterr().err


def test_replay_fold_detects_missing_obs_block(tmp_path: Path, capsys: Any) -> None:
    store, _ = _recorded(tmp_path, (6,))
    stub_id = store.load_fold_requests("s1")[0]["events"][0]["stub_id"]
    conn = sqlite3.connect(tmp_path / "obs.db")
    conn.execute("DELETE FROM blocks WHERE block_id = ?", ("obs_" + stub_id,))
    conn.commit()
    conn.close()
    assert replay_fold(tmp_path / "obs.db", "s1", expand=True) == 1
    assert "missing/changed" in capsys.readouterr().err


def test_replay_fold_cli_wiring(tmp_path: Path, capsys: Any) -> None:
    store, _ = _recorded(tmp_path, (6,))
    assert cfx_main(["replay-fold", "--store", str(tmp_path / "obs.db"), "--session", "s1"]) == 0
    assert "FOLD REPLAY OK" in capsys.readouterr().out
    # --expand emits the original unfolded request
    assert (
        cfx_main(
            ["replay-fold", "--store", str(tmp_path / "obs.db"), "--session", "s1", "--expand"]
        )
        == 0
    )
    assert '"role": "user"' in capsys.readouterr().out


# --------------------------------------------------------------------- sidecar E2E

_CAPTURED: list[bytes] = []


class _CaptureUpstream(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        _CAPTURED.append(body)
        payload = b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'
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


def _post(url: str, body: bytes) -> httpx.Response:
    return httpx.post(
        f"{url}/v1/chat/completions",
        content=body,
        headers={"Content-Type": "application/json", "X-Fabric-Agent": "s1"},
        timeout=10.0,
    )


def _stop(*servers: ThreadingHTTPServer) -> None:
    for server in servers:
        server.shutdown()
        server.server_close()


def test_e2e_replay_byte_identity_and_lossless_expansion(tmp_path: Path) -> None:
    _CAPTURED.clear()
    obs_store = open_store(tmp_path / "obs.db")
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureUpstream)
    sidecar = make_server(
        upstream=_serve(upstream),
        mode="passthrough",
        port=0,
        obs_fold=True,
        obs_store=obs_store,
    )
    sidecar_url = _serve(sidecar)
    sent: list[list[dict[str, Any]]] = []
    try:
        for count in (6, 7, 8, 9):
            messages = obs_messages(count)
            sent.append(messages)
            body = json.dumps({"model": "m", "messages": messages}).encode()
            assert _post(sidecar_url, body).status_code == 200
    finally:
        _stop(sidecar, upstream)

    assert len(_CAPTURED) == 4

    # Byte-identity: reconstruction reproduces every forwarded request exactly.
    bodies, drifts = fold_replay_bodies(obs_store, "s1")
    assert not drifts, drifts
    assert _CAPTURED == bodies

    # Lossless expansion: the unfolded messages equal what the client sent.
    expanded, drifts = fold_replay_bodies(obs_store, "s1", expand=True)
    assert not drifts, drifts
    for original, body in zip(sent, expanded, strict=True):
        assert json.loads(body)["messages"] == original
