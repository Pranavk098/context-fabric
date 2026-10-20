"""Terminal-Bench 2.1 adapter unit tests — offline, no Docker, no network.

Mocks the model with an in-process HTTP upstream (as ``tests/test_b8.py`` does) and mocks
the Harbor harness by building its command/config strings. Covers: category-stratified
sampling, the three hand-off arms (A/B/C) sharing a header but diverging, polyglot
tolerance of the B/C builders, line-numbered skim windows, the ``cfx serve`` and
``harbor run`` argv builders, and the model-routes-through-``cfx serve`` contract.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from evals.tbench import (
    AGENT,
    ARMS,
    DATASET,
    DEFAULT_N,
    WINDOW_CAP,
    build_handoff,
    build_task_header,
    harbor_command,
    sample_tasks,
    sidecar_command,
)
from sidecar import b8
from sidecar.server import make_server

# --------------------------------------------------------------------- fake tasks


def _tasks() -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for category, count in (
        ("system-administration", 8),
        ("software-engineering", 5),
        (
            "machine-learning",
            2,
        ),
    ):
        for i in range(count):
            tasks.append(
                {
                    "task_id": f"{category.replace('-', '_')}__{i}",
                    "instruction": f"task instruction {category} {i}",
                    "category": category,
                }
            )
    return tasks


# --------------------------------------------------------------------- fake upstream


class _EchoUpstream(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.dumps(
            {
                "id": "cmpl-test",
                "model": "gpt-4.1-mini",
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


def _write_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text("def fix_off_by_one():\n    return 1\n", encoding="utf-8")
    (repo / "Makefile").write_text("all:\n\tcc -o prog main.c\n", encoding="utf-8")
    (repo / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    return repo


# --------------------------------------------------------------------- sampling


def test_sample_tasks_is_deterministic_and_category_stratified() -> None:
    tasks = _tasks()
    a = sample_tasks(tasks, seed=20261019, n=10)
    b = sample_tasks(tasks, seed=20261019, n=10)
    assert [t["task_id"] for t in a] == [t["task_id"] for t in b]
    assert len(a) == 10
    # every category with >= 1 task holds a seat (floor rule)
    assert {t["category"] for t in a} == {
        "system-administration",
        "software-engineering",
        "machine-learning",
    }
    ids = [t["task_id"] for t in a]
    assert ids == sorted(ids)


def test_sample_tasks_respects_n_and_empty() -> None:
    tasks = _tasks()
    assert len(sample_tasks(tasks, seed=1, n=3)) == 3
    assert len(sample_tasks(tasks, seed=1, n=0)) == 0
    assert sample_tasks([], seed=1, n=5) == []


def test_sample_tasks_default_n_is_pilot() -> None:
    assert DEFAULT_N == 10


# --------------------------------------------------------------------- hand-off arms


def test_arms_share_header_and_diverge(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path)
    header = build_task_header("fix the off by one", "edit mod.py", "loop index")
    objective = "fix the off by one edit mod.py"
    tasks: dict[str, str] = {}
    for arm in ARMS:
        handoff = build_handoff(arm, repo, ["mod.py", "main.c"], objective)
        tasks[arm] = f"{header}\n\n{handoff}"
    for arm in ARMS:
        assert tasks[arm].startswith(header)
    assert tasks["A"][len(header) :] != tasks["B"][len(header) :]
    assert tasks["B"][len(header) :] != tasks["C"][len(header) :]
    # A is the frozen raw dump: whole file bodies in rank order.
    assert "===== FILE: mod.py =====" in tasks["A"]
    # B is a compile_packet: the Python symbol signature survives, the objective is echoed.
    assert "fix_off_by_one" in tasks["B"]
    # C opens with the shared repo map (Tier-0 hologram).
    assert "# Project Hologram" in tasks["C"]


def test_arm_b_is_polyglot_safe(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path)
    # main.c is non-Python: build_blocks (B8) would raise SyntaxError; arm B must not.
    # The objective shares a token ("main") so compile_packet seeds the block (it is a
    # retrieval compiler — irrelevant blocks are excluded, exactly as in B8).
    handoff = build_handoff("B", repo, ["main.c", "Makefile"], "compile the main program")
    assert "===== FILE: main.c =====" in handoff
    assert "int main" in handoff


def test_arm_c_line_windows_carry_line_numbers(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path)
    (repo / "big.py").write_text("\n".join(f"x{i} = {i}" for i in range(90)), encoding="utf-8")
    handoff = build_handoff("C", repo, ["big.py"], "x42 x43")
    # the 40-line window holding x42/x43 (L31-L70) is selected with 1-based line spans;
    # the irrelevant L1-L40 window is correctly excluded.
    assert "===== FILE: big.py (L31-L70) =====" in handoff
    assert "===== FILE: big.py (L1-L40) =====" not in handoff


def test_unknown_arm_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        build_handoff("D", _write_repo(tmp_path), ["mod.py"], "x")


def test_build_task_header_is_shared_prefix(tmp_path: Path) -> None:
    header = build_task_header("inst", "query", "notes")
    assert "===== TASK =====" in header
    assert "inst" in header and "query" in header and "notes" in header


# --------------------------------------------------------------------- argv builders


def test_sidecar_command_folds_only_arm_c() -> None:
    for arm in ARMS:
        argv = sidecar_command(8080, arm, ledger_dir="/tmp/ledger", daily_cap=2_200_000)
        assert "--mode" in argv and "passthrough" in argv
        assert "--window-cap" in argv and str(WINDOW_CAP) in argv
        assert "--b8-ledger-dir" in argv and "--b8-daily-cap" in argv
        assert ("--obs-fold" in argv) == (arm == "C")


def test_harbor_command_routes_through_cfx_serve() -> None:
    argv = harbor_command("gpt-4.1-mini", base_url="http://host.docker.internal:8080/v1")
    assert argv[0] == "harbor"
    assert argv[1] == "run"
    assert DATASET in argv
    assert AGENT in argv
    # v0.24.0 routes the endpoint via env (OPENAI_BASE_URL), with the openai/ model slug.
    assert "openai/gpt-4.1-mini" in argv
    assert "--ae" in argv and "OPENAI_BASE_URL=http://host.docker.internal:8080/v1" in argv
    assert "-e" in argv and "docker" in argv


# --------------------------------------------------------------------- model routing contract


def test_model_calls_route_through_cfx_serve_like_b8(tmp_path: Path) -> None:
    """The adapter's sidecar knobs (window cap + ledger + obs-fold) are the B8 choke point."""
    upstream, upstream_url = _start_upstream()
    ledger = b8.Ledger(tmp_path / "ledger")
    sidecar = make_server(
        upstream=upstream_url,
        mode="passthrough",
        port=0,
        window_cap=WINDOW_CAP,
        b8_ledger=ledger,
        obs_fold=True,
    )
    sidecar_url = _serve(sidecar)
    try:
        ok = httpx.post(
            f"{sidecar_url}/v1/chat/completions",
            json={"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "hi"}]},
            timeout=10.0,
        )
        assert ok.status_code == 200
        # the ledger recorded the upstream usage (the money cap is active)
        assert ledger.summary(b8.utc_day())["total_tokens"] == 8
        # an oversized prompt overflows the window cap exactly like B8
        over = httpx.post(
            f"{sidecar_url}/v1/chat/completions",
            json={"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "w " * 30000}]},
            timeout=10.0,
        )
        assert over.status_code == 400
        assert over.json()["error"]["code"] == "context_length_exceeded"
    finally:
        _stop(sidecar, upstream)
