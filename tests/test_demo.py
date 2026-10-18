"""``cfx demo`` tests — fake upstream, no GPU, no external network.

The fake upstream answers from the messages it receives: it "obeys" the written
negative-knowledge blocks, so pilot selection and control-vs-treated differences can be
asserted deterministically.
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from kernel.cartridge.serializer import count_tokens
from kernel.router.compiler import compile_packet
from kernel.store import SqliteStore
from protocol.models import BlockKind, LifecycleStatus, Recipient, Scope
from sidecar.demo import (
    broadcast_text,
    build_fixture_store,
    fleet_workload,
    main,
    negative_candidates,
    run_fleet,
    run_negative,
)
from sidecar.render import render_packet
from sidecar.repofixture import build_repo_fixture
from tests.test_repofixture import make_tiny_repo

_RECEIPT_KEYS = {"scenario", "model", "upstream", "started_at", "records", "arms", "cache", "notes"}

_FLEET_ANSWER = (
    "Project Atlas stores canonical state in Postgres with embeddings in pgvector; "
    "gRPC runs on port 7443; auth TTL is 120s; staging listens on 9091; "
    "p99 hit 820 ms; the API limit is 480/min."
)
_SAFE_ANSWER = "Use the built-in SQLite-backed alternative already shipped with Atlas."
_BAD_ANSWERS = {
    "search": "Adopt Elasticsearch for full-text search across Atlas blocks.",
    "queue": "Adopt Celery with Redis as the broker for Atlas background jobs.",
    "cache": "Use Redis as a caching layer in front of the Atlas API.",
    "metrics": "Deploy Prometheus with Grafana dashboards for observability.",
    "vector": "Adopt Pinecone for semantic search over past decisions.",
}


def _canned_text(request: dict[str, Any]) -> str:
    model = request.get("model")
    if model == "fail-model":
        return "I cannot answer."
    if model == "safe-model":
        return _SAFE_ANSWER
    blob = " ".join(str(m.get("content") or "") for m in request.get("messages", []))
    lowered = blob.lower()
    for candidate in negative_candidates():
        if candidate.objective.lower() not in lowered:
            continue
        if candidate.dead_end.lower() in lowered:  # treated arm saw the written dead end
            return _SAFE_ANSWER
        return _BAD_ANSWERS[candidate.trap_id]
    return _FLEET_ANSWER


class _FakeUpstream(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        text = _canned_text(request)
        prompt_chars = sum(len(str(m.get("content") or "")) for m in request.get("messages", []))
        payload = {
            "id": "cmpl-demo",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": max(prompt_chars // 4, 1),
                "completion_tokens": max(len(text) // 4, 1),
            },
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/metrics":
            body = (
                b"# HELP vllm:prefix_cache_hits_total test\n"
                b'vllm:prefix_cache_hits_total{model_name="fake"} 10.0\n'
                b'vllm:prefix_cache_queries_total{model_name="fake"} 100.0\n'
                b'vllm:kv_cache_usage_perc{model_name="fake"} 0.5\n'
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture()
def upstream() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture()
def client() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=10.0) as demo_client:
        yield demo_client


def test_fixture_floor_composition_and_compile_parity(tmp_path: Path) -> None:
    db = build_fixture_store(tmp_path / "f.db")
    blocks = SqliteStore(db).load_blocks()
    kinds = Counter(block.block_kind for block in blocks)
    assert kinds[BlockKind.decision] == 3
    assert kinds[BlockKind.negative_knowledge] == 2
    drafts = [b for b in blocks if b.lifecycle_status is LifecycleStatus.draft]
    assert len(blocks) > 15 and len(drafts) == 1
    blob_tokens = count_tokens(broadcast_text(blocks))
    assert blob_tokens > 5000, "fixture must stay large enough to make broadcast realistic"
    assert blob_tokens < 7500, "fixture must fit the 8192-token serving window"
    for spec in fleet_workload():
        result = compile_packet(
            scope=Scope(
                tenant_id="default", project_id="default", workflow_id="chat", task_id="chat"
            ),
            recipient=Recipient(agent_id=spec.role, role=spec.role, stage="chat"),
            objective=spec.objective,
            stage="chat",
            blocks=list(blocks),
            as_of=datetime.now(tz=UTC),
        )
        text = render_packet(result.packet)
        for required in spec.required:
            assert required.lower() in text.lower(), (spec.role, required)
        assert "PROHIBITIONS (approaches that failed" in text  # recency anchor is rendered
        assert "atlas_ledger_v2" not in text  # quarantined draft never routes


def test_fixture_is_fully_deterministic_across_builds(tmp_path: Path) -> None:
    first = SqliteStore(build_fixture_store(tmp_path / "a.db")).load_blocks()
    second = SqliteStore(build_fixture_store(tmp_path / "b.db")).load_blocks()
    assert first == second  # no wall-clock/RNG leaks into stored blocks
    assert broadcast_text(first) == broadcast_text(second)


def test_fleet_receipt_schema_and_symmetric_scoring(
    client: httpx.Client, upstream: str, tmp_path: Path
) -> None:
    receipt = run_fleet(client, upstream, model="fake-model", max_tokens=16, tmp_dir=tmp_path)
    assert set(receipt) == _RECEIPT_KEYS
    assert receipt["scenario"] == "fleet"
    assert set(receipt["arms"]) == {"broadcast", "compile"}
    for arm in ("broadcast", "compile"):
        assert receipt["arms"][arm]["passed"] == 6
        assert receipt["arms"][arm]["total"] == 6
    assert (
        receipt["arms"]["broadcast"]["prompt_tokens_total"]
        > receipt["arms"]["compile"]["prompt_tokens_total"]
    )
    assert all(record["passed"] for record in receipt["records"])
    assert receipt["cache"]["available"] is True
    broadcast_cache = receipt["cache"]["per_arm"]["broadcast"]
    assert broadcast_cache["prefix_cache_hits_delta"] == 0.0
    assert broadcast_cache["kv_cache_usage_peak"] == 0.5
    assert any("synthetic" in note for note in receipt["notes"])
    assert any("substring check" in note for note in receipt["notes"])
    for record in receipt["records"]:
        assert record["response"].startswith(record["response_text"])
        assert len(record["response_text"]) <= 500
        assert record["prompt_sha256"].startswith("sha256:")
    broadcast_hashes = {
        record["objective"]: record["prompt_sha256"]
        for record in receipt["records"]
        if record["arm"] == "broadcast"
    }
    compile_hashes = {
        record["objective"]: record["prompt_sha256"]
        for record in receipt["records"]
        if record["arm"] == "compile"
    }
    assert broadcast_hashes.keys() == compile_hashes.keys()
    assert all(broadcast_hashes[obj] != compile_hashes[obj] for obj in broadcast_hashes)


def test_fleet_failing_arm_fails_identically(
    client: httpx.Client, upstream: str, tmp_path: Path
) -> None:
    receipt = run_fleet(client, upstream, model="fail-model", max_tokens=16, tmp_dir=tmp_path)
    for arm in ("broadcast", "compile"):
        assert receipt["arms"][arm]["passed"] == 0
        assert receipt["arms"][arm]["total"] == 6
    assert all(not record["passed"] and record["missing"] for record in receipt["records"])


def test_negative_pilot_freezes_traps_and_measures_control_vs_treated(
    client: httpx.Client, upstream: str, tmp_path: Path
) -> None:
    receipt = run_negative(client, upstream, model="fake-model", max_tokens=16, tmp_dir=tmp_path)
    assert set(receipt) == _RECEIPT_KEYS
    assert receipt["scenario"] == "negative"
    pilot = [record for record in receipt["records"] if record["op"] == "pilot"]
    assert len(pilot) == 2 * len(negative_candidates())
    assert all(
        record["arm"] == "control" and record["bad"] and record["bad_v2"] and record["bad_v3"]
        for record in pilot
    )
    assert any("Frozen traps: search, queue" in note for note in receipt["notes"])
    assert receipt["arms"]["control"]["bad"] == 4
    assert receipt["arms"]["control"]["bad_v2"] == 4
    assert receipt["arms"]["control"]["bad_v3"] == 4
    assert receipt["arms"]["control"]["total"] == 4
    assert receipt["arms"]["control"]["per_trap"] == {
        "search": {"bad": 2, "bad_v2": 2, "bad_v3": 2, "total": 2},
        "queue": {"bad": 2, "bad_v2": 2, "bad_v3": 2, "total": 2},
    }
    assert receipt["arms"]["treated"]["bad"] == 0
    assert receipt["arms"]["treated"]["bad_v2"] == 0
    assert receipt["arms"]["treated"]["bad_v3"] == 0
    assert receipt["arms"]["treated"]["total"] == 4
    assert any("v2" in note and "affirmative" in note for note in receipt["notes"])
    assert any("NEGATIVE-SCORER.md" in note for note in receipt["notes"])
    writes = [record for record in receipt["records"] if record["op"] == "write"]
    assert len(writes) == 2
    assert all(record["status"] == 201 for record in writes)
    assert all(record["lifecycle_status"] == "routable" for record in writes)
    treated = SqliteStore(tmp_path / "treated.db")
    control = SqliteStore(tmp_path / "control.db")
    treated_ids = {block.block_id for block in treated.load_blocks()}
    control_ids = {block.block_id for block in control.load_blocks()}
    assert {record["block_id"] for record in writes} <= treated_ids
    assert {record["block_id"] for record in writes}.isdisjoint(control_ids)
    assert treated.count() == control.count() + 2
    chat = [record for record in receipt["records"] if record["op"] == "chat"]
    assert all(record["compiled"] for record in chat)
    assert all(record["response"].startswith(record["response_text"]) for record in chat)
    assert all(record["prompt_sha256"].startswith("sha256:") for record in chat)
    assert all(record["bad_v3"] == bool(record["bad_evidence_v3"]) for record in chat)


def test_negative_freeze_traps_skips_pilot_and_measures_named_traps(
    client: httpx.Client, upstream: str, tmp_path: Path
) -> None:
    receipt = run_negative(
        client,
        upstream,
        model="fake-model",
        max_tokens=16,
        tmp_dir=tmp_path,
        freeze_traps=["cache"],
    )
    assert not [record for record in receipt["records"] if record["op"] == "pilot"]
    assert any("Replication protocol" in note for note in receipt["notes"])
    assert not any("Pilot protocol" in note for note in receipt["notes"])
    assert set(receipt["arms"]) == {"control", "treated"}
    assert receipt["arms"]["control"]["bad"] == 2
    assert receipt["arms"]["control"]["bad_v2"] == 2
    assert receipt["arms"]["control"]["bad_v3"] == 2
    assert receipt["arms"]["treated"]["bad"] == 0
    assert receipt["arms"]["treated"]["bad_v2"] == 0
    assert receipt["arms"]["treated"]["bad_v3"] == 0
    writes = [record for record in receipt["records"] if record["op"] == "write"]
    assert len(writes) == 1
    assert writes[0]["status"] == 201
    assert writes[0]["lifecycle_status"] == "routable"


def test_main_rejects_freeze_traps_outside_negative_or_unknown_ids(
    upstream: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "never.json"
    rc = main(["--upstream", upstream, "--freeze-traps", "cache", "--out", str(out)])
    assert rc == 2
    assert "--freeze-traps requires --scenario negative" in capsys.readouterr().err
    rc = main(
        [
            "--upstream",
            upstream,
            "--scenario",
            "negative",
            "--freeze-traps",
            "cache,nope",
            "--out",
            str(out),
        ]
    )
    assert rc == 2
    assert "unknown trap id(s): nope" in capsys.readouterr().err
    assert not out.exists()


def test_negative_reports_null_result_when_pilot_finds_no_bad_control(
    client: httpx.Client, upstream: str, tmp_path: Path
) -> None:
    receipt = run_negative(client, upstream, model="safe-model", max_tokens=16, tmp_dir=tmp_path)
    assert receipt["arms"] == {}
    assert receipt["records"] and all(record["op"] == "pilot" for record in receipt["records"])
    assert any("NULL RESULT" in note for note in receipt["notes"])
    assert (
        not (tmp_path / "treated.db").exists()
        or SqliteStore(tmp_path / "treated.db").count()
        == SqliteStore(tmp_path / "control.db").count()
    )


def test_main_writes_valid_receipt_json(
    upstream: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "receipt.json"
    rc = main(
        [
            "--upstream",
            upstream,
            "--model",
            "fake-model",
            "--scenario",
            "fleet",
            "--out",
            str(out),
            "--max-tokens",
            "16",
        ]
    )
    assert rc == 0
    assert "scenario: fleet" in capsys.readouterr().out
    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data) == _RECEIPT_KEYS
    assert data["scenario"] == "fleet"


def test_priced_fleet_receipt_adds_cost_section(
    client: httpx.Client, upstream: str, tmp_path: Path
) -> None:
    receipt = run_fleet(
        client, upstream, model="fake-model", max_tokens=16, tmp_dir=tmp_path, price="openai:gpt-4o"
    )
    assert set(receipt) == _RECEIPT_KEYS | {"cost"}
    cost = receipt["cost"]
    assert cost["preset"] == "openai:gpt-4o"
    assert set(cost["per_arm"]) == {"broadcast", "compile"}
    assert cost["per_arm"]["compile"]["total_usd"] < cost["per_arm"]["broadcast"]["total_usd"]
    assert 0 < cost["savings_pct"] < 100
    assert any("list prices" in line for line in cost["assumptions"])
    for arm_cost in cost["per_arm"].values():
        assert arm_cost["total_usd"] > 0
        assert arm_cost["cached_tokens"] == 0  # static fake counters => zero delta
        assert arm_cost["per_request_usd"] == pytest.approx(arm_cost["total_usd"] / 6)


def test_priced_negative_receipt_costs_each_arm_without_savings(
    client: httpx.Client, upstream: str, tmp_path: Path
) -> None:
    receipt = run_negative(
        client,
        upstream,
        model="fake-model",
        max_tokens=16,
        tmp_dir=tmp_path,
        price="custom:2.5,10,1.25",
    )
    cost = receipt["cost"]
    assert cost["preset"] == "custom"
    assert set(cost["per_arm"]) == {"control", "treated"}
    assert "savings_pct" not in cost
    control = receipt["arms"]["control"]
    expected = control["prompt_tokens_total"] * 2.5 / 1_000_000 + (
        control["completion_tokens_total"] * 10 / 1_000_000
    )
    assert cost["per_arm"]["control"]["total_usd"] == pytest.approx(expected)


def test_mock_mode_runs_without_upstream_labels_and_prices(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "mock_receipt.json"
    rc = main(
        [
            "--mock",
            "--price",
            "openai:gpt-4o-mini",
            "--out",
            str(out),
            "--max-tokens",
            "16",
        ]
    )
    assert rc == 0
    printed = capsys.readouterr().out
    assert printed.splitlines()[0] == (
        "MOCK MODE — canned responses; accuracy is meaningless, this checks wiring/UX only"
    )
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["mock"] is True
    assert any("MOCK MODE" in note for note in data["notes"])
    assert set(data["arms"]) == {"broadcast", "compile"}
    assert all(arm["passed"] == 6 for arm in data["arms"].values())
    assert all(record["completion_tokens"] == 8 for record in data["records"])
    assert "cost" in data
    assert data["cost"]["preset"] == "openai:gpt-4o-mini"
    assert data["cost"]["savings_pct"] > 0
    assert all(cost["cached_tokens"] == 0 for cost in data["cost"]["per_arm"].values())
    blocks = SqliteStore(build_fixture_store(tmp_path / "fixture.db")).load_blocks()
    blob = broadcast_text(blocks)
    architect = fleet_workload()[0]
    record = next(
        record
        for record in data["records"]
        if record["arm"] == "broadcast" and record["agent"] == architect.role
    )
    assert record["prompt_tokens"] == count_tokens(blob) + count_tokens(architect.objective)


def test_main_rejects_unknown_price(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "never.json"
    rc = main(["--mock", "--price", "bogus", "--out", str(out)])
    assert rc == 2
    assert not out.exists()
    assert "unknown price preset" in capsys.readouterr().err


def test_repo_mode_mock_pipeline_records_real_path_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_tiny_repo(tmp_path / "repo")
    fixture = build_repo_fixture(repo)
    out = tmp_path / "repo_receipt.json"
    rc = main(["--mock", "--repo", str(repo), "--out", str(out), "--max-tokens", "16"])
    assert rc == 0
    assert capsys.readouterr().out.splitlines()[0] == (
        "MOCK MODE — canned responses; accuracy is meaningless, this checks wiring/UX only"
    )
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["mock"] is True
    assert data["scenario"] == "repo"
    assert data["fixture"] == {
        "kind": "repo",
        "path": str(repo),
        "files": len(fixture.files),
        "raw_tokens": fixture.raw_tokens,
    }
    assert set(data["arms"]) == {"raw", "compile"}
    assert all(arm["requests"] == 6 and arm["total"] == 6 for arm in data["arms"].values())
    raw_records = [record for record in data["records"] if record["arm"] == "raw"]
    assert len(raw_records) == 6
    assert [record["required"] for record in raw_records] == [
        list(objective.keys) for objective in fixture.objectives
    ]
    for record in raw_records:
        (key,) = record["required"]
        assert (repo / key).is_file()
    assert len({record["objective"] for record in raw_records}) == 6
    first = raw_records[0]
    assert first["prompt_tokens"] == count_tokens(fixture.bundle) + count_tokens(first["objective"])
    assert data["cache"]["available"] is False


def test_repo_mode_priced_receipt_uses_raw_baseline(tmp_path: Path) -> None:
    repo = make_tiny_repo(tmp_path / "repo")
    out = tmp_path / "repo_cost.json"
    rc = main(
        [
            "--mock",
            "--repo",
            str(repo),
            "--price",
            "openai:gpt-4o-mini",
            "--out",
            str(out),
            "--max-tokens",
            "16",
        ]
    )
    assert rc == 0
    cost = json.loads(out.read_text(encoding="utf-8"))["cost"]
    assert cost["preset"] == "openai:gpt-4o-mini"
    assert set(cost["per_arm"]) == {"raw", "compile"}
    assert isinstance(cost["savings_pct"], float)
    assert all(arm_cost["cached_tokens"] == 0 for arm_cost in cost["per_arm"].values())


def test_main_rejects_repo_with_negative_scenario_or_bad_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_tiny_repo(tmp_path / "repo")
    out = tmp_path / "never.json"
    rc = main(["--mock", "--repo", str(repo), "--scenario", "negative", "--out", str(out)])
    assert rc == 2
    assert "--scenario negative" in capsys.readouterr().err
    rc = main(["--mock", "--repo", str(tmp_path / "missing"), "--out", str(out)])
    assert rc == 2
    assert "not a directory" in capsys.readouterr().err
    assert not out.exists()


def test_main_repo_with_no_python_files_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = main(["--mock", "--repo", str(empty), "--out", str(tmp_path / "never.json")])
    assert rc == 2
    assert "no Python files found" in capsys.readouterr().err
