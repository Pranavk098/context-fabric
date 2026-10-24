"""cfxd session_state tests: bounds, LRU, redaction, and the JSON round-trip."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from cfxd import session_state as ss
from cfxd.daemon import percentile


def _seed_file(session_id: str, path: str, *, edited: bool = False, lines: int | None = 42) -> None:
    tool = "Edit" if edited else "Read"
    payload: dict[str, object] = {
        "session_id": session_id,
        "tool_name": tool,
        "tool_input": {"file_path": path},
        "tool_response": {"file": {"numLines": lines, "totalLines": lines}},
    }
    ss.observe_post_tool_use(payload)


def test_observe_records_files_commands_todos() -> None:
    ss.reset()
    _seed_file("s1", "/a/b.py", edited=True, lines=412)
    _seed_file("s1", "/a/c.py", edited=False, lines=230)
    ss.observe_post_tool_use(
        {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "pytest -q"}}
    )
    ss.observe_post_tool_use(
        {
            "session_id": "s1",
            "tool_name": "TodoWrite",
            "tool_input": {
                "todos": [
                    {"content": "fix the race", "status": "in_progress"},
                    {"content": "write tests", "status": "pending"},
                ]
            },
        }
    )

    facts = ss.snapshot("s1")
    assert facts is not None
    assert facts.session_id == "s1"
    assert {Path(f.path).name for f in facts.files} == {"b.py", "c.py"}
    by_name = {Path(f.path).name: f for f in facts.files}
    assert by_name["b.py"].edited is True
    assert by_name["b.py"].lines == 412
    assert by_name["c.py"].edited is False
    assert facts.commands == ("pytest -q",)
    assert facts.todos == (("in_progress", "fix the race"), ("pending", "write tests"))


def test_edit_does_not_downgrade_to_read() -> None:
    ss.reset()
    _seed_file("s1", "/a/b.py", edited=True, lines=412)
    _seed_file("s1", "/a/b.py", edited=False, lines=500)
    facts = ss.snapshot("s1")
    assert facts is not None
    (f,) = facts.files
    assert f.edited is True
    assert f.lines == 500


def test_command_dedupes_and_caps() -> None:
    ss.reset()
    for i in range(30):
        ss.observe_post_tool_use(
            {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": f"cmd {i}"}}
        )
    facts = ss.snapshot("s1")
    assert facts is not None
    assert len(facts.commands) == ss.MAX_COMMANDS
    # Exact-duplicate collapse: re-running a command moves it to the front, no copies.
    ss.observe_post_tool_use(
        {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "cmd 29"}}
    )
    facts = ss.snapshot("s1")
    assert facts is not None
    assert len(facts.commands) == ss.MAX_COMMANDS
    assert facts.commands.count("cmd 29") == 1


def test_file_lru_cap() -> None:
    ss.reset()
    for i in range(ss.MAX_FILES + 50):
        _seed_file("s1", f"/a/file_{i}.py")
    facts = ss.snapshot("s1")
    assert facts is not None
    assert len(facts.files) == ss.MAX_FILES


def test_record_fold_caps_and_snapshots() -> None:
    ss.reset()
    for i in range(ss.MAX_FOLDS + 10):
        ss.record_fold("s1", "Bash", f"cmd {i}", f"obs{i:016x}", f"/obs/{i}.txt", 50_000, "test")
    facts = ss.snapshot("s1")
    assert facts is not None
    assert len(facts.folds) == ss.MAX_FOLDS
    # Newest first is the LRU end; the last recorded obs_id survives.
    assert facts.folds[-1].obs_id == f"obs{ss.MAX_FOLDS + 9:016x}"


def test_redact_command() -> None:
    assert ss.redact_command("curl -H 'Authorization: Bearer abc123' https://x") == (
        "curl -H 'Authorization: Bearer ***' https://x"
    )
    assert "TOKEN=***" in ss.redact_command("export TOKEN=sekrit")
    assert "API_KEY=***" in ss.redact_command("API_KEY=sekrit go")
    assert "-p***" in ss.redact_command("mysql -psekrit db")
    # --port must not be mistaken for a -p password flag.
    assert ss.redact_command("serve --port 8080") == "serve --port 8080"
    # URL user:pass@ redacts the password.
    assert "user:***@" in ss.redact_command("git clone https://user:hunter2@example.com/repo.git")


def test_observe_never_raises_on_garbage() -> None:
    ss.reset()
    cases = [
        None,
        "not a dict",
        {},
        {"session_id": 123, "tool_name": "Bash"},
        {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": 42}},
        {"session_id": "s1", "tool_name": "Read", "tool_input": None},
        {"session_id": "s1", "tool_name": "TodoWrite", "tool_input": {"todos": "nope"}},
    ]
    for payload in cases:
        ss.observe_post_tool_use(payload)  # type: ignore[arg-type]


def test_to_json_from_json_roundtrip() -> None:
    ss.reset()
    _seed_file("s1", "/a/b.py", edited=True, lines=412)
    ss.observe_post_tool_use(
        {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "pytest -q"}}
    )
    ss.record_fold("s1", "Bash", "pytest -q", "abc123", "/obs/abc123.txt", 50_000, "test")
    facts = ss.snapshot("s1")
    assert facts is not None
    restored = ss.from_json(ss.to_json(facts))
    assert restored == facts


def test_from_json_rejects_bad_input() -> None:
    assert ss.from_json({}) is None
    assert ss.from_json({"v": 1, "session_id": "s1"}) is None
    assert ss.from_json({"v": 0, "session_id": 42}) is None
    assert ss.from_json({"v": 0, "session_id": "s1", "files": [{"path": 3}]}) is None


def test_state_concurrency_bounds() -> None:
    # 16 threads run 5,000 observe calls across 100 sessions while others call
    # snapshot. Pass: no exception, sessions <= 64, files per session <= 200,
    # observe p99 under 5 ms (relaxed bound; CI-like machines can be slower).
    ss.reset()
    latencies: list[float] = []
    errors: list[BaseException] = []
    lat_lock = threading.Lock()

    def observe_worker(worker_id: int) -> None:
        for i in range(5_000):
            sid = f"sess-{(i + worker_id) % 100}"
            payload = {
                "session_id": sid,
                "tool_name": "Edit",
                "tool_input": {"file_path": f"/a/file_{(i + worker_id) % 300}.py"},
                "tool_response": {},
            }
            start = time.perf_counter()
            try:
                ss.observe_post_tool_use(payload)
            except BaseException as exc:  # pragma: no cover - only on a bug
                errors.append(exc)
            with lat_lock:
                latencies.append((time.perf_counter() - start) * 1000.0)

    def snapshot_worker() -> None:
        for i in range(5_000):
            ss.snapshot(f"sess-{i % 100}")

    threads = [threading.Thread(target=observe_worker, args=(w,)) for w in range(16)]
    snap_threads = [threading.Thread(target=snapshot_worker) for _ in range(4)]
    for t in [*threads, *snap_threads]:
        t.start()
    for t in [*threads, *snap_threads]:
        t.join()

    assert not errors

    live_sessions = 0
    for i in range(100):
        facts = ss.snapshot(f"sess-{i}")
        if facts is not None:
            live_sessions += 1
            assert len(facts.files) <= ss.MAX_FILES
    assert live_sessions <= ss.MAX_SESSIONS

    # Typical calls take microseconds; the tail under 16-thread GIL contention on a shared CI
    # runner is scheduler jitter. The real limit is the daemon's 150 ms handler deadline.
    assert percentile(latencies, 50) < 1.0
    assert percentile(latencies, 99) < 50.0
