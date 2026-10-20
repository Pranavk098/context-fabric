"""B8 unit tests — model allow-list, daily-cap stop/resume, window-cap error shape,
sampler determinism, and resume skip. No network, no GPU, no Docker."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from evals.b8_aggregate import aggregate, mcnemar_p, verdicts
from evals.b8_live import (
    DEFAULT_N,
    FROZEN_INSTANCE_IDS,
    _arm_predictions_path,
    _bm25_file_listing,
    _classify_exception,
    _is_cap_error,
    _is_transient_http_error,
    _pin_dataset_offline,
    _print_report,
    _salvage_git_diff,
    _sidecar_env,
    append_run,
    assert_frozen_sample,
    assert_single_tooling_rerun,
    build_handoff,
    build_subagent_task,
    build_task_header,
    chat_completions_url,
    finished_pairs,
    grader_run_id,
    list_python_files,
    localize_parent,
    pair_fits,
    parent_handoff,
    pending_pairs,
    recover_patch,
    sample_instances,
    stale_grader_report_paths,
    tooling_failure_reruns,
    tooling_failures,
)
from sidecar import b8
from sidecar.server import make_server

_DAY = "2026-10-19"


@pytest.fixture(autouse=True)
def _dummy_api_key(monkeypatch) -> None:
    # The parent localization now sends OPENAI_API_KEY as an Authorization header; give
    # tests a dummy so they reach the mock upstream instead of failing on a missing key.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy")


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
        # A 400 (not 429/5xx) is non-retryable by OpenAI clients and maps to a clean stop.
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "daily_cap_reached"
        assert "Resume tomorrow" in error["message"]
    finally:
        _stop(sidecar, upstream)


def test_daily_cap_records_usage_after_proxy(tmp_path: Path) -> None:
    # AU3-M1: usage is recorded BEFORE the response body is written, so by the time the
    # client observes a complete response the ledger already holds the tokens (no delta race).
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


# -------------------------------------------------------------------- cap retry / resume


def test_is_cap_error_recognizes_daily_cap_reached() -> None:
    class _FakeExc(Exception):
        pass

    assert _is_cap_error(_FakeExc("BadRequestError: ... code 'daily_cap_reached' ..."))
    assert _is_cap_error(_FakeExc("... exceeding CF_B8_DAILY_CAP=600000 ..."))
    assert not _is_cap_error(_FakeExc("BadRequestError: some other malformed request"))
    assert not _is_cap_error(_FakeExc("ContextWindowExceededError"))


def test_cap_400_maps_to_bad_request_not_rate_limit(tmp_path: Path) -> None:
    """A daily-cap stop returns 400 -> litellm.BadRequestError (aborted), never a retried 429."""
    litellm = pytest.importorskip("litellm")
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
        with pytest.raises(litellm.exceptions.BadRequestError):
            litellm.completion(
                model="gpt-4.1-mini",
                messages=[{"role": "user", "content": "hi"}],
                api_base=f"{sidecar_url}/v1",
                custom_llm_provider="openai",
                api_key="sk-test-not-real",
                num_retries=0,
            )
    finally:
        _stop(sidecar, upstream)


# -------------------------------------------------------------------- grading schema


def test_make_test_spec_accepts_verified_schema() -> None:
    """``make_test_spec`` succeeds on the ``SWE-bench/SWE-bench_Verified`` row shape.

    The ``princeton-nlp`` mirror dropped ``eval_script``/``log_parser``/``eval_type``; the
    grader pins the ``SWE-bench/...`` snapshot that still carries them.
    """
    utils = pytest.importorskip("swebench.harness.utils")
    instance = {
        "instance_id": "astropy__astropy-7606",
        "image": "swebench/sweb.eval.x86_64.astropy_1776_astropy-7606:latest",
        "repo": "astropy/astropy",
        "version": "4.1",
        "FAIL_TO_PASS": '["astropy/units/tests/test_units.py::test_xyz"]',
        "PASS_TO_PASS": '["astropy/units/tests/test_units.py::test_abc"]',
        "log_parser": "parse_log_astropy",
        "eval_type": "pass_and_fail",
        "eval_script": (
            "#!/bin/bash\nset -uxo pipefail\ncd /testbed\n"
            "pytest astropy/units/tests/test_units.py\n"
        ),
    }
    spec = utils.make_test_spec(instance)
    assert spec.instance_id == "astropy__astropy-7606"
    assert spec.eval_script_list  # parse_eval_script produced a non-empty script list
    assert spec.log_parser == "parse_log_astropy"
    assert spec.eval_type == "pass_and_fail"


# -------------------------------------------------------------------- parent localization


_PARENT_OK = json.dumps(
    {
        "candidate_files": ["mod.py"],
        "subagent_query": "Fix the off-by-one in mod.py.",
        "parent_notes": "Index error in the loop.",
    }
)

_INSTANCE = {"problem_statement": "the counter is off by one"}


class _ParentUpstream(BaseHTTPRequestHandler):
    """Returns ``content`` (a class attribute) as the parent-model reply."""

    content = _PARENT_OK

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.dumps(
            {
                "id": "cmpl-parent",
                "model": "gpt-4.1-mini",
                "choices": [{"message": {"role": "assistant", "content": type(self).content}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 9, "total_tokens": 20},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        pass


class _UnparseableParentUpstream(_ParentUpstream):
    content = "this is not a JSON object"


class _EmptyQueryParentUpstream(_ParentUpstream):
    content = json.dumps({"candidate_files": ["mod.py"], "subagent_query": "  "})


class _ServerErrorUpstream(_ParentUpstream):
    """A parent-model upstream that fails every turn with a 500 (transient transport fault)."""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.dumps({"error": {"message": "boom", "code": "server_error"}}).encode()
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _start_upstream_with(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    return server, _serve(server)


def _write_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "other.py").write_text("x = 1\n", encoding="utf-8")
    return repo


def _start_parent_sidecar(upstream_url: str, tmp_path: Path) -> tuple[ThreadingHTTPServer, str]:
    ledger = b8.Ledger(tmp_path / "ledger")
    sidecar = make_server(upstream=upstream_url, mode="passthrough", port=0, b8_ledger=ledger)
    return sidecar, _serve(sidecar)


def test_chat_completions_url_joins_v1_once() -> None:
    assert chat_completions_url("http://127.0.0.1:8080/v1") == (
        "http://127.0.0.1:8080/v1/chat/completions"
    )
    assert chat_completions_url("http://127.0.0.1:8080") == (
        "http://127.0.0.1:8080/v1/chat/completions"
    )
    assert chat_completions_url("http://127.0.0.1:8080/v1/") == (
        "http://127.0.0.1:8080/v1/chat/completions"
    )


def test_parent_localization_is_llm_ok_through_sidecar(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream_with(_ParentUpstream)
    sidecar, sidecar_url = _start_parent_sidecar(upstream_url, tmp_path)
    try:
        # base_url ending in /v1 must not double to /v1/v1 (the D1 bug).
        result = localize_parent(
            _INSTANCE, _write_repo(tmp_path), model="gpt-4.1-mini", base_url=f"{sidecar_url}/v1"
        )
        assert result["parent_status"] == "llm_ok"
        assert result["subagent_query"].strip()
        assert "mod.py" in result["candidate_files"]
    finally:
        _stop(sidecar, upstream)


def test_parent_unparseable_reply_is_llm_failed_not_bm25(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream_with(_UnparseableParentUpstream)
    sidecar, sidecar_url = _start_parent_sidecar(upstream_url, tmp_path)
    try:
        result = localize_parent(
            _INSTANCE, _write_repo(tmp_path), model="gpt-4.1-mini", base_url=sidecar_url
        )
        assert result["parent_status"] == "llm_failed"
        assert result["error"]
        assert result["subagent_query"] == ""
        assert result["candidate_files"] == []  # never a BM25-only hand-off
    finally:
        _stop(sidecar, upstream)


def test_parent_empty_query_reply_is_llm_failed(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream_with(_EmptyQueryParentUpstream)
    sidecar, sidecar_url = _start_parent_sidecar(upstream_url, tmp_path)
    try:
        result = localize_parent(
            _INSTANCE, _write_repo(tmp_path), model="gpt-4.1-mini", base_url=sidecar_url
        )
        assert result["parent_status"] == "llm_failed"
        assert result["candidate_files"] == []
    finally:
        _stop(sidecar, upstream)


def test_parent_rejects_disallowed_model(tmp_path: Path) -> None:
    upstream, upstream_url = _start_upstream_with(_ParentUpstream)
    sidecar, sidecar_url = _start_parent_sidecar(upstream_url, tmp_path)
    try:
        result = localize_parent(
            _INSTANCE, _write_repo(tmp_path), model="gpt-4o", base_url=f"{sidecar_url}/v1"
        )
        assert result["parent_status"] == "llm_failed"
        assert "HTTPStatusError" in result["error"]
    finally:
        _stop(sidecar, upstream)


# ------------------------------------------------------- parent auth header (deviation D2)


def test_parent_reply_sends_authorization_header(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-12345")
    captured: list[str | None] = []

    class _Capturing(_ParentUpstream):
        def do_POST(self) -> None:
            captured.append(self.headers.get("Authorization"))
            super().do_POST()

    upstream, upstream_url = _start_upstream_with(_Capturing)
    sidecar, sidecar_url = _start_parent_sidecar(upstream_url, tmp_path)
    try:
        result = localize_parent(
            _INSTANCE, _write_repo(tmp_path), model="gpt-4.1-mini", base_url=f"{sidecar_url}/v1"
        )
        assert result["parent_status"] == "llm_ok"
        assert captured == ["Bearer sk-test-secret-12345"]
    finally:
        _stop(sidecar, upstream)


def test_parent_missing_key_is_llm_failed_with_clear_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    upstream, upstream_url = _start_upstream_with(_ParentUpstream)
    sidecar, sidecar_url = _start_parent_sidecar(upstream_url, tmp_path)
    try:
        result = localize_parent(
            _INSTANCE, _write_repo(tmp_path), model="gpt-4.1-mini", base_url=f"{sidecar_url}/v1"
        )
        assert result["parent_status"] == "llm_failed"
        assert "OPENAI_API_KEY" in result["error"]
        assert result["candidate_files"] == []
    finally:
        _stop(sidecar, upstream)


def test_parent_key_never_written_to_cache(tmp_path: Path, monkeypatch) -> None:
    key = "sk-test-secret-12345"
    monkeypatch.setenv("OPENAI_API_KEY", key)
    upstream, upstream_url = _start_upstream_with(_ParentUpstream)
    sidecar, sidecar_url = _start_parent_sidecar(upstream_url, tmp_path)
    cache_dir = tmp_path / "cache"
    instance = {**_INSTANCE, "instance_id": "test__0"}
    try:
        result = parent_handoff(
            instance,
            _write_repo(tmp_path),
            model="gpt-4.1-mini",
            base_url=f"{sidecar_url}/v1",
            cache_dir=cache_dir,
        )
        assert result["parent_status"] == "llm_ok"
        cache_text = (cache_dir / "test__0.json").read_text(encoding="utf-8")
        assert key not in cache_text
    finally:
        _stop(sidecar, upstream)


# ------------------------------------------------------- grading no-crash (deviation D2)


def test_arm_predictions_path_missing_file_is_none(tmp_path: Path) -> None:
    assert _arm_predictions_path(tmp_path / "nope.jsonl", "A") is None


def test_arm_predictions_path_filters_by_arm(tmp_path: Path) -> None:
    predictions = tmp_path / "b8_predictions.jsonl"
    predictions.write_text(
        json.dumps({"instance_id": "x", "model_patch": "p", "arm": "A"})
        + "\n"
        + json.dumps({"instance_id": "y", "model_patch": "q", "arm": "B"})
        + "\n",
        encoding="utf-8",
    )
    per_arm = _arm_predictions_path(predictions, "A")
    assert per_arm is not None
    lines = per_arm.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["instance_id"] == "x"
    # an arm with no rows is "nothing to grade", not an error
    assert _arm_predictions_path(predictions, "C") is None


# ------------------------------------------------------------------- audit B8 harness fixes


def test_task_header_identical_across_arms(tmp_path: Path) -> None:
    """F1: A/B/C share the identical task header; they differ only after it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "other.py").write_text("x = 1\n", encoding="utf-8")
    problem = "the counter is off by one"
    query = "Fix the off-by-one in mod.py."
    notes = "Index error in the loop."
    header = build_task_header(problem, query, notes)
    assert "===== TASK =====" in header
    assert problem in header and query in header and notes in header
    tasks: dict[str, str] = {}
    for arm in ("A", "B", "C"):
        handoff = build_handoff(
            arm, repo, ["mod.py", "other.py"], f"{problem} {query}", project_id="repo"
        )
        tasks[arm] = build_subagent_task(header, handoff)
    for arm in ("A", "B", "C"):
        assert tasks[arm].startswith(header), f"arm {arm} must share the header prefix"
    assert tasks["A"][len(header) :] != tasks["B"][len(header) :]
    assert tasks["B"][len(header) :] != tasks["C"][len(header) :]


def test_bm25_file_listing_ranks_and_signs(tmp_path: Path) -> None:
    """F2: the parent listing is BM25-ranked with a signature, not alphabetical."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "aaa.py").write_text("def unrelated():\n    pass\n", encoding="utf-8")
    (repo / "target.py").write_text("def fix_off_by_one():\n    return 1\n", encoding="utf-8")
    listing = _bm25_file_listing(repo, list_python_files(repo), "fix off by one")
    assert listing[0].startswith("target.py:")
    assert "def fix_off_by_one" in listing[0]
    assert any(line.startswith("aaa.py:") for line in listing)


def test_parent_cap_stopped_is_not_cached(tmp_path: Path) -> None:
    """F14: a cap hit during the parent phase is not cached, so the task retries next day."""
    ledger = b8.Ledger(tmp_path / "ledger")
    ledger.append(b8.utc_day(), model="gpt-4.1-mini", prompt_tokens=990, completion_tokens=0)
    upstream, upstream_url = _start_upstream_with(_ParentUpstream)
    sidecar = make_server(
        upstream=upstream_url, mode="passthrough", port=0, b8_ledger=ledger, b8_daily_cap=1000
    )
    sidecar_url = _serve(sidecar)
    cache_dir = tmp_path / "cache"
    instance = {**_INSTANCE, "instance_id": "cap__0"}
    try:
        result = parent_handoff(
            instance,
            _write_repo(tmp_path),
            model="gpt-4.1-mini",
            base_url=f"{sidecar_url}/v1",
            cache_dir=cache_dir,
        )
        assert result["parent_status"] == "cap_stopped"
        assert not (cache_dir / "cap__0.json").exists()
    finally:
        _stop(sidecar, upstream)


def test_parent_cache_records_provenance(tmp_path: Path) -> None:
    """F2/F14: the parent cache carries model, prompt hash, sha, timestamp, listing."""
    upstream, upstream_url = _start_upstream_with(_ParentUpstream)
    sidecar, sidecar_url = _start_parent_sidecar(upstream_url, tmp_path)
    cache_dir = tmp_path / "cache"
    instance = {**_INSTANCE, "instance_id": "prov__0"}
    try:
        result = parent_handoff(
            instance,
            _write_repo(tmp_path),
            model="gpt-4.1-mini",
            base_url=f"{sidecar_url}/v1",
            cache_dir=cache_dir,
        )
        assert result["parent_status"] == "llm_ok"
        cached = json.loads((cache_dir / "prov__0.json").read_text(encoding="utf-8"))
        assert cached["model"] == "gpt-4.1-mini"
        assert "created_at" in cached
        assert cached["harness_sha"]  # non-empty in a git checkout
        assert cached["prompt_sha256"]
        assert "listing_shown" in cached
    finally:
        _stop(sidecar, upstream)


class _DupCandidateUpstream(_ParentUpstream):
    content = json.dumps(
        {
            "candidate_files": ["mod.py", "mod.py", "other.py"],
            "subagent_query": "Fix it.",
            "parent_notes": "n",
        }
    )


def test_parent_dedupes_candidate_files(tmp_path: Path) -> None:
    """F9: a duplicate candidate path no longer consumes a candidate seat."""
    upstream, upstream_url = _start_upstream_with(_DupCandidateUpstream)
    sidecar, sidecar_url = _start_parent_sidecar(upstream_url, tmp_path)
    try:
        result = localize_parent(
            _INSTANCE, _write_repo(tmp_path), model="gpt-4.1-mini", base_url=f"{sidecar_url}/v1"
        )
        assert result["parent_status"] == "llm_ok"
        assert result["candidate_files"].count("mod.py") == 1
        assert "other.py" in result["candidate_files"]
    finally:
        _stop(sidecar, upstream)


def test_pair_fits_reserves_before_starting() -> None:
    """F3: a pair starts only when the remaining budget covers --pair-reserve."""
    assert pair_fits(0, 550_000, 2_200_000)
    assert pair_fits(1_650_000, 550_000, 2_200_000)
    assert not pair_fits(1_650_001, 550_000, 2_200_000)


def test_print_report_skips_tokenless_rows(capsys: pytest.CaptureFixture[str]) -> None:
    """F4: an earlier-day resolved-only row (no tokens) must not crash the report."""
    receipt = {
        "arms": {"A": {"earlier": {"resolved": 0}}, "B": {}, "C": {}},
        "ledger": {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0},
        "day": "2026-10-20",
    }
    _print_report(receipt, Path("out.json"))  # must not raise KeyError
    assert "tasks=0" in capsys.readouterr().out


def test_recover_patch_is_uniform_across_exit_statuses() -> None:
    """F5: one patch-recovery rule for every non-cap exit status."""
    assert recover_patch("cap_stopped", "", "diff") == ("", "")
    assert recover_patch("Submitted", "patch", "diff") == ("patch", "agent_submission")
    assert recover_patch("LimitsExceeded", "", "diff") == ("diff", "working_tree_git_diff")
    assert recover_patch("ContextWindowExceededError", "", "diff") == (
        "diff",
        "working_tree_git_diff",
    )
    assert recover_patch("Submitted", "", "") == ("", "")


def test_salvage_git_diff_reads_working_tree() -> None:
    class _Env:
        def execute(self, command: dict[str, str]) -> dict[str, Any]:
            assert command == {"command": "git diff"}
            return {"output": "diff --git a/x b/x", "returncode": 0}

    assert _salvage_git_diff(_Env()) == "diff --git a/x b/x"

    class _Broken:
        def execute(self, command: dict[str, str]) -> dict[str, Any]:
            raise RuntimeError("boom")

    assert _salvage_git_diff(_Broken()) == ""


def test_aggregate_excludes_non_results(tmp_path: Path) -> None:
    """F6: R/O/U exclude cap_stopped and parent_failed; latest row wins."""
    i1, i2 = FROZEN_INSTANCE_IDS[0], FROZEN_INSTANCE_IDS[1]
    runs = tmp_path / "runs.jsonl"
    reports = tmp_path / "reports"
    reports.mkdir()
    append_run(
        runs,
        {
            "instance_id": i1,
            "arm": "A",
            "exit_status": "Submitted",
            "overflow": False,
            "tokens": {"uncached_tokens": 100},
        },
    )
    append_run(
        runs,
        {
            "instance_id": i2,
            "arm": "A",
            "exit_status": "ContextWindowExceededError",
            "overflow": True,
            "tokens": {"uncached_tokens": 200},
        },
    )
    append_run(
        runs,
        {
            "instance_id": FROZEN_INSTANCE_IDS[2],
            "arm": "A",
            "exit_status": "cap_stopped",
            "overflow": False,
            "tokens": {"uncached_tokens": 999},
        },
    )
    append_run(
        runs,
        {
            "instance_id": FROZEN_INSTANCE_IDS[3],
            "arm": "A",
            "exit_status": "parent_failed",
            "overflow": False,
            "tokens": {"uncached_tokens": 999},
        },
    )
    (reports / "gpt-4.1-mini.b8v3_A.json").write_text(
        json.dumps({"resolved_ids": [i1], "completed_ids": [i1, i2]}), encoding="utf-8"
    )
    result = aggregate(runs, "gpt-4.1-mini", reports)
    assert result["arms"]["A"]["R"] == 1
    assert result["arms"]["A"]["O"] == 1
    assert result["arms"]["A"]["U"] == 300  # 100 + 200; the non-results are excluded
    assert result["arms"]["A"]["tasks"] == 2


def test_tooling_failures_latest_row_wins(tmp_path: Path) -> None:
    """F12: a re-run supersedes a tooling_failure; the flag gates re-runs."""
    runs = tmp_path / "runs.jsonl"
    append_run(runs, {"instance_id": "x", "arm": "A", "exit_status": "tooling_failure"})
    assert tooling_failures(runs) == {("x", "A")}
    append_run(runs, {"instance_id": "x", "arm": "A", "exit_status": "Submitted"})
    assert tooling_failures(runs) == set()


def test_mcnemar_exact() -> None:
    assert mcnemar_p(0, 0) == 1.0
    assert mcnemar_p(3, 0) == pytest.approx(0.25)
    assert mcnemar_p(0, 3) == pytest.approx(0.25)


def test_stream_rejected_when_ledger_on(tmp_path: Path) -> None:
    """AU3-H1: stream=true is refused with a clear 400 when the ledger is on."""
    ledger = b8.Ledger(tmp_path / "ledger")
    upstream, upstream_url = _start_upstream()
    sidecar = make_server(upstream=upstream_url, mode="passthrough", port=0, b8_ledger=ledger)
    sidecar_url = _serve(sidecar)
    try:
        response = httpx.post(
            f"{sidecar_url}/v1/chat/completions",
            json={"model": "gpt-4.1-mini", "messages": _messages("hi"), "stream": True},
            timeout=10.0,
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "stream_not_ledgered"
    finally:
        _stop(sidecar, upstream)


def test_fold_stats_surfaced_in_sidecar_stats(tmp_path: Path) -> None:
    """F7: fold events and folded bytes are counted and exposed at /fabric/stats."""
    upstream, upstream_url = _start_upstream()
    sidecar = make_server(upstream=upstream_url, mode="passthrough", port=0, obs_fold=True)
    sidecar_url = _serve(sidecar)
    try:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "fix it"},
        ]
        for i in range(6):
            messages.append({"role": "assistant", "content": f"calling {i}"})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": f"call_{i}",
                    "content": "line one\n" + " ".join(f"b{i}_{j}" for j in range(50)),
                }
            )
        response = _post(sidecar_url, {"model": "gpt-4.1-mini", "messages": messages})
        assert response.status_code == 200
        stats = httpx.get(f"{sidecar_url}/fabric/stats", timeout=10.0).json()
        assert stats["folded_events"] >= 3
        assert stats["folded_bytes"] > 0
    finally:
        _stop(sidecar, upstream)


def test_window_cap_estimate_counts_tools_and_framing() -> None:
    """F8: the window-cap estimate adds the tools schema and per-message framing."""
    from kernel.cartridge.serializer import count_tokens

    tools = [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]
    messages = [{"role": "user", "content": "hello world"}]
    base = b8.estimate_messages_tokens(messages)
    with_tools = b8.estimate_messages_tokens(messages, tools=tools)
    assert with_tools - base == count_tokens(json.dumps(tools))
    assert base == count_tokens("hello world") + b8._FRAME_TOKENS
    # The old 4-token framing undercounted the recorded first arm-A request by 40 tokens
    # (9,223 vs 9,183); the raised framing must not undercount.
    assert b8._FRAME_TOKENS >= 7


def test_ledger_reads_and_appends_are_thread_safe(tmp_path: Path) -> None:
    """F11/M1: concurrent appends and summaries do not interleave or lose rows."""
    ledger = b8.Ledger(tmp_path / "ledger")
    day = b8.utc_day()

    def appender() -> None:
        for _ in range(50):
            ledger.append(day, model="gpt-4.1-mini", prompt_tokens=1, completion_tokens=0)

    threads = [threading.Thread(target=appender) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ledger.summary(day)["total_tokens"] == 200


def test_utc_day_boundary() -> None:
    """F10: the UTC day is computed per pair, and the boundary is correct."""
    from datetime import UTC, datetime

    assert b8.utc_day(datetime(2026, 10, 19, 23, 59, tzinfo=UTC)) == "2026-10-19"
    assert b8.utc_day(datetime(2026, 10, 20, 0, 0, tzinfo=UTC)) == "2026-10-20"


def test_assert_frozen_sample_guards_drift() -> None:
    """F15: the seeded n=10 draw is asserted against the frozen list."""
    assert_frozen_sample([{"instance_id": i} for i in FROZEN_INSTANCE_IDS], 20261019, 10)
    with pytest.raises(SystemExit):
        assert_frozen_sample([{"instance_id": "something__else"}] * 10, 20261019, 10)


def test_transient_exception_is_tooling_failure_not_finished() -> None:
    """AU4-13/d: a transient network/API fault maps to tooling_failure, not a prediction."""

    class APIConnectionError(Exception):
        pass

    class ContextWindowExceededError(Exception):
        pass

    assert _classify_exception(APIConnectionError("connection reset")) == (
        "tooling_failure",
        "APIConnectionError: connection reset",
    )
    assert _classify_exception(ContextWindowExceededError("window full")) == (
        "ContextWindowExceededError",
        "",
    )
    assert _classify_exception(Exception("daily_cap_reached ... CF_B8_DAILY_CAP=1")) == (
        "cap_stopped",
        "",
    )


def test_sqlite_store_close_checkpoints_wal(tmp_path: Path) -> None:
    """AU5-a: closing the store checkpoints the WAL, so no data is left only in -wal."""
    from kernel.store.sqlite_store import SqliteStore
    from protocol.models import BlockKind, ContextBlock, Creator, CreatorType, Fidelity, MemoryTier

    db = tmp_path / "obs.db"
    store = SqliteStore(db, check_same_thread=False)
    store.append(
        ContextBlock(
            block_id="b1",
            block_kind=BlockKind.observation,
            memory_tier=MemoryTier.register,
            content="folded content",
            content_hash="sha256:b1",
            token_estimate=3,
            source_event_seq=1,
            creator=Creator(type=CreatorType.tool, id="t"),
            fidelity=Fidelity.raw,
            trust_domain="default",
        )
    )
    store.close()
    # A clean close checkpoints the WAL into the main file and removes/empties the -wal.
    wal = tmp_path / "obs.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0
    reopened = SqliteStore(db)
    assert reopened.count() == 1
    reopened.close()


# ------------------------------------------------------------------- audit B8 round-3


def test_grader_run_id_is_v3_specific() -> None:
    """B1: grading uses a v3-specific run_id that cannot collide with the voided v2 ``b8_*``."""
    assert grader_run_id("A") == "b8v3_A"
    assert grader_run_id("B") == "b8v3_B"
    assert grader_run_id("C") == "b8v3_C"


def test_stale_grader_report_refused(tmp_path: Path) -> None:
    """B1: grade() refuses when an official report.json already exists for an instance."""
    logs_dir = tmp_path / "logs" / "run_evaluation"
    report = logs_dir / "b8v3_A" / "gpt-4.1-mini" / "x" / "report.json"
    report.parent.mkdir(parents=True)
    report.write_text("{}", encoding="utf-8")
    assert stale_grader_report_paths("b8v3_A", "gpt-4.1-mini", ["x", "y"], logs_dir=logs_dir) == [
        report
    ]
    assert stale_grader_report_paths("b8v3_A", "gpt-4.1-mini", ["y"], logs_dir=logs_dir) == []


def test_aggregate_keeps_only_frozen_sample_ids(tmp_path: Path) -> None:
    """B2: aggregate() drops non-frozen ids, so a smoke row cannot land in R/O/U."""
    frozen = FROZEN_INSTANCE_IDS[0]
    runs = tmp_path / "runs.jsonl"
    reports = tmp_path / "reports"
    reports.mkdir()
    for iid in (frozen, "astropy__astropy-7606"):
        append_run(
            runs,
            {
                "instance_id": iid,
                "arm": "A",
                "exit_status": "Submitted",
                "overflow": False,
                "tokens": {"uncached_tokens": 50},
            },
        )
    (reports / "gpt-4.1-mini.b8v3_A.json").write_text(
        json.dumps({"resolved_ids": [frozen], "completed_ids": [frozen, "astropy__astropy-7606"]}),
        encoding="utf-8",
    )
    result = aggregate(runs, "gpt-4.1-mini", reports)
    assert result["arms"]["A"]["tasks"] == 1
    assert result["arms"]["A"]["R"] == 1


def test_aggregate_missing_grade_is_tooling_failure(tmp_path: Path) -> None:
    """N1: a finished row with no official grade becomes a tooling failure, not a 0."""
    frozen = FROZEN_INSTANCE_IDS[0]
    runs = tmp_path / "runs.jsonl"
    reports = tmp_path / "reports"
    reports.mkdir()
    append_run(
        runs,
        {
            "instance_id": frozen,
            "arm": "A",
            "exit_status": "Submitted",
            "overflow": False,
            "tokens": {"uncached_tokens": 50},
        },
    )
    (reports / "gpt-4.1-mini.b8v3_A.json").write_text(
        json.dumps({"resolved_ids": [], "completed_ids": []}), encoding="utf-8"
    )
    result = aggregate(runs, "gpt-4.1-mini", reports)
    assert result["arms"]["A"]["tasks"] == 0
    assert result["tooling"]["A"] == {frozen}


def test_verdict_rules() -> None:
    """N1: the §5 verdicts (including ceil(O(A)/2)) are computed in code."""
    from evals.b8_aggregate import _verdict

    a = {"R": 2, "O": 4, "U": 1000, "tasks": 10}
    assert _verdict({"R": 5, "O": 4, "U": 2000, "tasks": 10}, a, 10) == "STRONG SIGNAL"
    # SIGNAL: R >= A and O <= ceil(4/2)=2 and U <= 0.75*1000
    assert _verdict({"R": 2, "O": 2, "U": 750, "tasks": 10}, a, 10) == "SIGNAL"
    assert _verdict({"R": 0, "O": 4, "U": 1000, "tasks": 10}, a, 10) == "NEGATIVE"
    assert _verdict({"R": 2, "O": 3, "U": 800, "tasks": 10}, a, 10) == "INCONCLUSIVE"


def test_verdict_with_no_paired_tasks_is_inconclusive() -> None:
    """Zero paired tasks satisfy 0 >= 0 and 0 <= 0.75*0 vacuously; that must not be SIGNAL."""
    from evals.b8_aggregate import _verdict

    zero = {"R": 0, "O": 0, "U": 0, "tasks": 0}
    assert _verdict(zero, zero, 0) == "INCONCLUSIVE"


def test_verdicts_paired_intersection() -> None:
    """N1: verdicts use the paired intersection; unpaired tasks are reported as drops."""
    outcomes = {
        "A": {
            "t1": {"resolved": True, "overflow": False, "uncached": 100},
            "t2": {"resolved": False, "overflow": True, "uncached": 200},
        },
        "B": {
            "t1": {"resolved": True, "overflow": False, "uncached": 100},
            "t2": {"resolved": True, "overflow": False, "uncached": 100},
            "t3": {"resolved": True, "overflow": False, "uncached": 1},
        },
        "C": {},
    }
    b = verdicts(outcomes)[("B", "A")]
    assert b["n"] == 2  # only t1 and t2 are paired; t3 is a drop from B
    assert b["dropped_x"] == ["t3"]
    assert b["dropped_a"] == []
    assert b["verdict"] in ("STRONG SIGNAL", "SIGNAL", "NEGATIVE", "INCONCLUSIVE")


def test_tooling_failure_rerun_refused_after_one(tmp_path: Path) -> None:
    """N2: two tooling_failure rows mean the single allowed re-run is already spent."""
    runs = tmp_path / "runs.jsonl"
    append_run(runs, {"instance_id": "x", "arm": "A", "exit_status": "tooling_failure"})
    assert tooling_failure_reruns(runs) == {("x", "A"): 1}
    append_run(runs, {"instance_id": "x", "arm": "A", "exit_status": "tooling_failure"})
    assert tooling_failure_reruns(runs) == {("x", "A"): 2}
    with pytest.raises(SystemExit):
        assert_single_tooling_rerun(runs)


def test_parent_transient_5xx_is_llm_retry_not_cached(tmp_path: Path) -> None:
    """N3: a transient 5xx parent failure is retried later, not cached as llm_failed."""
    upstream, upstream_url = _start_upstream_with(_ServerErrorUpstream)
    sidecar, sidecar_url = _start_parent_sidecar(upstream_url, tmp_path)
    cache_dir = tmp_path / "cache"
    instance = {**_INSTANCE, "instance_id": "transient__0"}
    try:
        result = parent_handoff(
            instance,
            _write_repo(tmp_path),
            model="gpt-4.1-mini",
            base_url=f"{sidecar_url}/v1",
            cache_dir=cache_dir,
        )
        assert result["parent_status"] == "llm_retry"
        assert result["error"]
        assert not (cache_dir / "transient__0.json").exists()
    finally:
        _stop(sidecar, upstream)


def test_is_transient_http_error() -> None:
    """N3: the transient classifier separates transport faults from bad replies."""
    assert _is_transient_http_error(httpx.TimeoutException("timeout"))
    assert _is_transient_http_error(httpx.ConnectError("connect"))
    assert not _is_transient_http_error(ValueError("bad reply"))
    request = httpx.Request("POST", "http://localhost/v1")
    server_error = httpx.HTTPStatusError(
        "server error", request=request, response=httpx.Response(500, request=request)
    )
    assert _is_transient_http_error(server_error)
    client_error = httpx.HTTPStatusError(
        "auth", request=request, response=httpx.Response(401, request=request)
    )
    assert not _is_transient_http_error(client_error)


def test_grader_pins_offline_dataset(monkeypatch: pytest.MonkeyPatch) -> None:
    """N7: the grader forces the HF loader offline so it reads the pinned cached snapshot."""
    monkeypatch.delenv("HF_DATASETS_OFFLINE", raising=False)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    _pin_dataset_offline()
    assert os.environ["HF_DATASETS_OFFLINE"] == "1"
    assert os.environ["HF_HUB_OFFLINE"] == "1"


def test_default_n_is_pilot_10() -> None:
    """N8: the default run is the n=10 pilot, so the frozen-sample assert fires by default."""
    assert DEFAULT_N == 10


def test_sidecar_env_scrubs_cf_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """N9: the spawned sidecar inherits no CF_* defaults the runner did not set explicitly."""
    monkeypatch.setenv("CF_OBS_FOLD", "1")
    monkeypatch.setenv("CF_FABRIC", "/tmp/f")
    monkeypatch.setenv("KEEP_ME", "yes")
    env = _sidecar_env()
    assert "CF_OBS_FOLD" not in env
    assert "CF_FABRIC" not in env
    assert env["KEEP_ME"] == "yes"


def test_print_report_excludes_tooling_failure(capsys: pytest.CaptureFixture[str]) -> None:
    """N10: a tooling_failure row with tokens is excluded from the arm totals."""
    receipt = {
        "arms": {
            "A": {
                "ok": {
                    "exit_status": "Submitted",
                    "overflow": False,
                    "steps": 1,
                    "wall_time_s": 1.0,
                    "tokens": {"total_tokens": 10, "cached_tokens": 2, "uncached_tokens": 8},
                },
                "boom": {
                    "exit_status": "tooling_failure",
                    "overflow": False,
                    "steps": 0,
                    "wall_time_s": 0.0,
                    "tokens": {"total_tokens": 999, "cached_tokens": 0, "uncached_tokens": 999},
                },
            },
            "B": {},
            "C": {},
        },
        "ledger": {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0},
        "day": "2026-10-20",
    }
    _print_report(receipt, Path("out.json"))
    out = capsys.readouterr().out
    assert "billed=10" in out
    assert "excluded=1" in out
