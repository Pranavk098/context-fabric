"""OpenCode reader tests: counts/sizes only, read-only, canaries, CLI, schema fallback."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import cfxd.agents.opencode as opencode
from cfxd.agents import build_report, default_root, format_human, since_cutoff
from cfxd.agents.opencode import OpenCodeDBError, read_sessions

ROOT = Path(__file__).resolve().parents[1]

_NOW = datetime(2026, 1, 15, tzinfo=UTC)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _v1_schema(with_parent_id: bool = True) -> list[str]:
    parent = "parent_id TEXT, " if with_parent_id else ""
    return [
        "CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, workspace_id TEXT, "
        f"{parent}slug TEXT, directory TEXT, path TEXT, title TEXT, version TEXT, "
        "cost REAL, tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER, "
        "tokens_cache_read INTEGER, tokens_cache_write INTEGER, agent TEXT, model TEXT, "
        "time_created INTEGER, time_updated INTEGER, time_compacting INTEGER, "
        "time_archived INTEGER)",
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, "
        "time_updated INTEGER, data TEXT)",
        "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, "
        "time_created INTEGER, time_updated INTEGER, data TEXT)",
    ]


def _v2_schema() -> list[str]:
    return [
        "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, parent_id TEXT, time_created INTEGER, "
        "time_updated INTEGER)",
        "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, "
        "seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT)",
    ]


def _mkdb(path: Path, with_parent_id: bool = True) -> None:
    con = sqlite3.connect(path)
    for stmt in _v1_schema(with_parent_id) + _v2_schema():
        con.execute(stmt)
    con.commit()
    con.close()


def _session(id: str, parent: str | None, updated: datetime) -> tuple:
    return (id, parent, _ms(updated), _ms(updated))


def _assistant(tokens: dict) -> str:
    return json.dumps({"role": "assistant", "time": {"created": _ms(_NOW)}, "tokens": tokens})


def _tool(tool: str, output: str) -> str:
    return json.dumps(
        {
            "type": "tool",
            "tool": tool,
            "callID": "c",
            "state": {"status": "completed", "output": output},
        }
    )


def _compaction() -> str:
    return json.dumps(
        {"type": "compaction", "auto": False, "overflow": False, "tail_start_id": "x"}
    )


def _build_main(tmp_path: Path) -> Path:
    """Both families; parent+child+old sessions, usage, tools, a compaction."""
    db = tmp_path / "opencode.db"
    _mkdb(db)
    con = sqlite3.connect(db)
    recent = _NOW - timedelta(days=1)
    old = _NOW - timedelta(days=40)

    con.executemany(
        "INSERT INTO session (id, parent_id, time_created, time_updated) VALUES (?, ?, ?, ?)",
        [
            ("sess_parent", None, _ms(recent), _ms(recent)),
            ("sess_child", "sess_parent", _ms(recent), _ms(recent)),
            ("sess_old", None, _ms(old), _ms(old)),
        ],
    )
    # v2 family also has data; the reader must prefer session/message/part.
    con.execute(
        "INSERT INTO session_v2 (id, parent_id, time_created, time_updated) VALUES (?, ?, ?, ?)",
        ("v2_sess", None, _ms(recent), _ms(recent)),
    )
    con.execute(
        "INSERT INTO session_message (id, session_id, type, seq, time_created, data) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("v2_msg", "v2_sess", "message", 1, _ms(recent), "{}"),
    )

    con.executemany(
        "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
        [
            (
                "m1",
                "sess_parent",
                _ms(recent),
                _assistant(
                    {"input": 100, "output": 50, "reasoning": 10, "cache": {"read": 20, "write": 5}}
                ),
            ),
            (
                "m2",
                "sess_parent",
                _ms(recent),
                _assistant(
                    {"input": 200, "output": 30, "reasoning": 0, "cache": {"read": 40, "write": 0}}
                ),
            ),
            (
                "m3",
                "sess_parent",
                _ms(recent),
                json.dumps({"role": "user", "time": {"created": _ms(recent)}}),
            ),
            (
                "m4",
                "sess_child",
                _ms(recent),
                _assistant(
                    {"input": 50, "output": 10, "reasoning": 0, "cache": {"read": 0, "write": 0}}
                ),
            ),
            (
                "m5",
                "sess_old",
                _ms(old),
                _assistant(
                    {
                        "input": 999,
                        "output": 999,
                        "reasoning": 999,
                        "cache": {"read": 999, "write": 999},
                    }
                ),
            ),
        ],
    )
    con.executemany(
        "INSERT INTO part (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
        [
            ("p1", "sess_parent", _ms(recent), _tool("bash", "hello world")),
            ("p2", "sess_parent", _ms(recent), _tool("read", "abc")),
            ("p3", "sess_parent", _ms(recent), _tool("bash", "\u2603ab")),  # 5 bytes, 3 chars
            ("p4", "sess_parent", _ms(recent), _compaction()),
            ("p5", "sess_child", _ms(recent), _tool("bash", "xy")),
        ],
    )
    con.commit()
    con.close()
    return db


def test_counts_and_byte_sums(tmp_path: Path) -> None:
    db = _build_main(tmp_path)
    report = build_report("opencode", read_sessions(db, since_cutoff("30d", _NOW)))
    assert report["sessions"] == 1
    assert report["subagent_sessions"] == 1
    assert report["turns"]["total"] == 3
    assert report["usage"]["input_uncached"] == 350
    assert report["usage"]["cache_read"] == 60
    assert report["usage"]["cache_write"] == 5
    assert report["usage"]["output"] == 90
    assert report["usage"]["reasoning"] == 10
    # Anthropic-style weighting: 350 + 1.25*5 + 0.1*60 + 5*90
    assert report["usage"]["weighted_tokens"] == pytest.approx(812.25, abs=0.051)
    assert report["tools"]["bash"] == {
        "count": 3,
        "bytes": 18,
        "p50": 5.0,
        "p90": 9.8,
        "max": 11,
        "over_10k": 0,
        "over_25k": 0,
    }
    assert report["tools"]["read"] == {
        "count": 1,
        "bytes": 3,
        "p50": 3.0,
        "p90": 3.0,
        "max": 3,
        "over_10k": 0,
        "over_25k": 0,
    }
    assert report["compactions"]["count"] == 1


def test_since_all_includes_old_session(tmp_path: Path) -> None:
    db = _build_main(tmp_path)
    report = build_report("opencode", read_sessions(db, None))
    assert report["sessions"] == 2
    assert report["subagent_sessions"] == 1
    assert report["turns"]["total"] == 4
    assert report["usage"]["input_uncached"] == 350 + 999


def test_root_may_be_directory(tmp_path: Path) -> None:
    _build_main(tmp_path)
    report = build_report("opencode", read_sessions(tmp_path, None))
    assert report["sessions"] == 2


def test_read_only_connection(tmp_path: Path) -> None:
    db = _build_main(tmp_path)
    con = opencode._connect(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("CREATE TABLE should_fail (x INTEGER)")
    finally:
        con.close()


def test_degraded_missing_parent_id_and_tokens(tmp_path: Path) -> None:
    db = tmp_path / "degraded.db"
    _mkdb(db, with_parent_id=False)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO session (id, time_created, time_updated) VALUES (?, ?, ?)",
        ("sess_a", _ms(_NOW), _ms(_NOW)),
    )
    # tokens missing cache and reasoning (older row shape)
    con.execute(
        "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
        (
            "m1",
            "sess_a",
            _ms(_NOW),
            json.dumps({"role": "assistant", "tokens": {"input": 70, "output": 7}}),
        ),
    )
    con.commit()
    con.close()

    report = build_report("opencode", read_sessions(db, None))
    assert report["subagent_sessions"] == 0  # parent_id column absent -> not sub-agent
    assert report["usage"]["input_uncached"] == 70
    assert report["usage"]["output"] == 7
    assert report["usage"]["cache_read"] == 0
    assert report["usage"]["cache_write"] == 0
    assert report["usage"]["reasoning"] == 0


def test_v2_only_unsupported(tmp_path: Path) -> None:
    db = tmp_path / "v2.db"
    con = sqlite3.connect(db)
    for stmt in _v2_schema():
        con.execute(stmt)
    con.execute(
        "INSERT INTO session_v2 (id, parent_id, time_created, time_updated) VALUES (?, ?, ?, ?)",
        ("v2_sess", None, _ms(_NOW), _ms(_NOW)),
    )
    con.execute(
        "INSERT INTO session_message (id, session_id, type, seq, time_created, data) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("v2_msg", "v2_sess", "message", 1, _ms(_NOW), "{}"),
    )
    con.commit()
    con.close()

    with pytest.raises(OpenCodeDBError, match="session_v2"):
        list(read_sessions(db, None))


def test_empty_database(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    con = sqlite3.connect(db)
    con.commit()
    con.close()

    report = build_report("opencode", read_sessions(db, None))
    assert report["sessions"] == 0
    assert report["subagent_sessions"] == 0
    assert "No opencode sessions found" in format_human(report, db.name)


def test_canary_strings_never_leak(tmp_path: Path) -> None:
    canaries = ("CANARYPATH-1111", "CANARYCMD-2222", "CANARYPROMPT-3333", "CANARYOUT-4444")
    db = tmp_path / "canary.db"
    _mkdb(db)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO session (id, parent_id, directory, path, title, time_created, time_updated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("sess_a", None, f"C:\\u\\{canaries[0]}", canaries[0], canaries[0], _ms(_NOW), _ms(_NOW)),
    )
    con.execute(
        "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
        (
            "m1",
            "sess_a",
            _ms(_NOW),
            json.dumps(
                {
                    "role": "assistant",
                    "tokens": {
                        "input": 1,
                        "output": 1,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                    "modelID": canaries[0],
                }
            ),
        ),
    )
    con.execute(
        "INSERT INTO part (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
        ("p1", "sess_a", _ms(_NOW), json.dumps({"type": "text", "text": canaries[2]})),
    )
    con.execute(
        "INSERT INTO part (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
        (
            "p2",
            "sess_a",
            _ms(_NOW),
            _tool(f"C:\\secret\\{canaries[0]}", f"cmd {canaries[1]} output {canaries[3]}"),
        ),
    )
    con.commit()
    con.close()

    for extra in ([], ["--json"]):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "cfxd",
                "agent-audit",
                "--agent",
                "opencode",
                "--root",
                str(db),
                *extra,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        for canary in canaries:
            assert canary not in result.stdout
            assert canary not in result.stderr


def test_cli_json_and_missing_db(tmp_path: Path) -> None:
    db = _build_main(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cfxd",
            "agent-audit",
            "--agent",
            "opencode",
            "--root",
            str(db),
            "--since",
            "all",
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["agent"] == "opencode"
    assert data["sessions"] == 2
    assert data["subagent_sessions"] == 1
    assert data["turns"]["total"] == 4
    assert "session/message/part" in result.stderr  # which schema was used

    missing = subprocess.run(
        [
            sys.executable,
            "-m",
            "cfxd",
            "agent-audit",
            "--agent",
            "opencode",
            "--root",
            str(tmp_path / "nope.db"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert missing.returncode == 1
    assert "not found" in missing.stderr


def test_default_root_opencode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENCODE_DB", str(tmp_path / "custom.db"))
    assert default_root("opencode") == tmp_path / "custom.db"

    monkeypatch.delenv("OPENCODE_DB", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert default_root("opencode") == tmp_path / "opencode" / "opencode.db"
