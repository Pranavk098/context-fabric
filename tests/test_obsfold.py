"""Observation folding tests — mask policy, reversibility, prefix stability, window guard,
receipt, and the sidecar flag-off byte-identity guarantee."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from kernel.store import SqliteStore
from protocol.canonical import canonicalize
from sidecar.cli import main as cfx_main
from sidecar.obsfold import DEFAULT_K, DEFAULT_N, estimate_tokens, expand, fold_messages
from sidecar.server import make_server, open_store

AS_OF = datetime(2026, 1, 1, tzinfo=UTC)


def obs_messages(count: int, *, body_tokens: int = 40) -> list[dict[str, Any]]:
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
                "content": f"line one of file {i}\n"
                + " ".join(f"body{i}_{j}" for j in range(body_tokens)),
            }
        )
    return messages


def tool_indices(messages: list[dict[str, Any]]) -> list[int]:
    return [i for i, m in enumerate(messages) if m.get("role") == "tool"]


def render(messages: list[dict[str, Any]]) -> bytes:
    """Newline-joined canonical bytes — a proxy for the rendered prompt the model sees."""
    return b"\n".join(canonicalize(message) for message in messages)


# ------------------------------------------------------------------- mask policy


def test_mask_keeps_last_n_verbatim() -> None:
    msgs = obs_messages(6)
    result = fold_messages(msgs, n=3, k=3)
    assert result.folded == 3
    indices = tool_indices(msgs)  # [3, 5, 7, 9, 11, 13]
    for index in indices[:3]:
        assert result.messages[index]["content"].startswith("[ctx://obs/")
    for index in indices[3:]:
        assert result.messages[index]["content"] == msgs[index]["content"]
    # system and user are never touched
    assert result.messages[0] == msgs[0]
    assert result.messages[1] == msgs[1]
    # stub shape
    stub = result.messages[indices[0]]["content"]
    assert stub.startswith("[ctx://obs/") and stub.endswith("]")
    assert "folded: read_file" in stub and " bytes, " in stub


def test_stubs_are_reversible(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "obs.db")
    msgs = obs_messages(4)
    result = fold_messages(msgs, n=0, k=1, store=store, created_at=AS_OF)
    assert result.folded == 4
    for event in result.events:
        original = msgs[event.message_index]["content"]
        assert expand(store, f"ctx://obs/{event.stub_id}") == original
        assert expand(store, event.stub_id) == original  # bare id form


# ------------------------------------------------------------- prefix stability


def test_prefix_stability_between_folds() -> None:
    r6 = render(fold_messages(obs_messages(6), n=3, k=3).messages)
    r7 = render(fold_messages(obs_messages(7), n=3, k=3).messages)
    r8 = render(fold_messages(obs_messages(8), n=3, k=3).messages)
    # within a k-chunk window the leading bytes of consecutive requests are identical
    assert r7.startswith(r6)
    assert r8.startswith(r7)
    # the fold boundary only advances at k multiples: 9 observations fold 6
    assert fold_messages(obs_messages(9), n=3, k=3).folded == 6


# --------------------------------------------------------------- window guard


def test_window_guard_folds_oldest_first_until_fits() -> None:
    msgs = obs_messages(10, body_tokens=120)
    max_tokens = estimate_tokens(obs_messages(3, body_tokens=120))
    result = fold_messages(msgs, n=3, k=3, max_context_tokens=max_tokens)
    assert estimate_tokens(result.messages) <= max_tokens
    indices = tool_indices(msgs)
    folded = [i for i in indices if result.messages[i]["content"].startswith("[ctx://obs/")]
    verbatim = [i for i in indices if i not in folded]
    assert folded == indices[: len(folded)]  # oldest first
    assert verbatim == indices[len(folded) :]
    assert folded and len(folded) > 3  # the guard folded past the mask's 3-chunk boundary
    assert result.messages[0]["content"] == msgs[0]["content"]
    assert result.messages[1]["content"] == msgs[1]["content"]


def test_window_guard_never_touches_system_or_latest_user() -> None:
    msgs = obs_messages(8, body_tokens=120)
    unfoldable = [m for m in msgs if m["role"] != "tool"]
    max_tokens = estimate_tokens(unfoldable)  # even folding every tool cannot fit
    result = fold_messages(msgs, n=0, k=1, max_context_tokens=max_tokens)
    assert result.folded == 8
    for index, message in enumerate(msgs):
        if message["role"] != "tool":
            assert result.messages[index] == message


# --------------------------------------------------------------------- receipt


def test_receipt_contains_fold_events(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "obs.db")
    msgs = obs_messages(5)
    result = fold_messages(msgs, n=2, k=1, store=store, session_id="s1", created_at=AS_OF)
    assert result.folded == 3
    log = store.load_fold_log(session_id="s1")
    assert len(log) == 3
    for event, row in zip(result.events, log, strict=False):
        assert row["session_id"] == "s1"
        assert row["message_index"] == event.message_index
        assert row["stub_id"] == event.stub_id
        assert row["content_hash"] == event.content_hash
        assert row["tool_name"] == event.tool_name
        assert row["content_bytes"] == event.content_bytes
        assert row["seen_hash"] == result.seen_hash
        assert row["created_at"] == AS_OF.isoformat()


def test_replay_reconstructs_what_model_saw(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "obs.db")
    msgs = obs_messages(5)
    first = fold_messages(msgs, n=2, k=1, store=store, session_id="s1", created_at=AS_OF)
    replayed = fold_messages(msgs, n=2, k=1)  # no store: pure re-derivation
    assert replayed.messages == first.messages
    assert replayed.seen_hash == first.seen_hash
    assert replayed.events == first.events
    for event in first.events:
        assert expand(store, f"ctx://obs/{event.stub_id}") == msgs[event.message_index]["content"]


# ------------------------------------------------------------------- sidecar flag


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
        headers={"Content-Type": "application/json"},
        timeout=10.0,
    )


def _stop(*servers: ThreadingHTTPServer) -> None:
    for server in servers:
        server.shutdown()
        server.server_close()


def test_flag_off_forwards_body_byte_identical(tmp_path: Path) -> None:
    _CAPTURED.clear()
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureUpstream)
    sidecar = make_server(upstream=_serve(upstream), mode="passthrough", port=0)  # flag off
    sidecar_url = _serve(sidecar)
    body = json.dumps({"model": "m", "messages": obs_messages(6)}).encode()
    try:
        assert _post(sidecar_url, body).status_code == 200
    finally:
        _stop(sidecar, upstream)
    assert _CAPTURED == [body], "flag off must forward the request body byte-identically"


def test_flag_on_folds_observations_and_expands(tmp_path: Path) -> None:
    _CAPTURED.clear()
    obs_store = open_store(tmp_path / "obs.db")
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureUpstream)
    sidecar = make_server(
        upstream=_serve(upstream),
        mode="passthrough",
        port=0,
        obs_fold=True,
        obs_store=obs_store,
        obs_n=DEFAULT_N,
        obs_k=DEFAULT_K,
    )
    sidecar_url = _serve(sidecar)
    msgs = obs_messages(6)
    body = json.dumps({"model": "m", "messages": msgs}).encode()
    try:
        assert _post(sidecar_url, body).status_code == 200
    finally:
        _stop(sidecar, upstream)
    forwarded = json.loads(_CAPTURED[0])
    folded = forwarded["messages"]
    assert folded[0] == msgs[0]  # system untouched
    assert folded[1] == msgs[1]  # user untouched
    indices = tool_indices(msgs)
    for index in indices[:3]:
        assert folded[index]["content"].startswith("[ctx://obs/")
    for index in indices[3:]:
        assert folded[index]["content"] == msgs[index]["content"]
    # the folded content is persisted and expandable
    stub = folded[indices[0]]["content"]
    uri = stub[1 : stub.index(" folded:")]
    assert expand(obs_store, uri) == msgs[indices[0]]["content"]


# --------------------------------------------------------------------- CLI expand


def test_expand_cli_prints_content(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = SqliteStore(tmp_path / "obs.db")
    msgs = obs_messages(1)
    result = fold_messages(msgs, n=0, k=1, store=store, created_at=AS_OF)
    uri = "ctx://obs/" + result.events[0].stub_id

    assert cfx_main(["expand", "--store", str(tmp_path / "obs.db"), uri]) == 0
    assert capsys.readouterr().out == msgs[3]["content"] + "\n"

    assert cfx_main(["expand", "--store", str(tmp_path / "obs.db"), "ctx://obs/deadbeefdead"]) == 1
    assert "not found" in capsys.readouterr().err
