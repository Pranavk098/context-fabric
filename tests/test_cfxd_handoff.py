"""Cross-agent handoff tests: packet build from hand-built logs, import, render, AGENTS.md."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cfxd import carry, checkpoint, handoff, packet
from cfxd import session_state as ss
from cfxd.handlers import clear

_ROOT = Path(__file__).resolve().parents[1]
_TS = "2026-01-02T00:00:00Z"
_AT = 1767312000.0


@pytest.fixture(autouse=True)
def _clean() -> Any:
    ss.reset()
    clear()
    checkpoint.reset()
    yield
    ss.reset()
    clear()
    checkpoint.reset()


# --- hand-built Codex rollout ---------------------------------------------


def _line(ordinal: int, type_: str, payload: dict[str, Any], ts: str = _TS) -> dict[str, Any]:
    return {"timestamp": ts, "ordinal": ordinal, "type": type_, "payload": payload}


def _call(
    ordinal: int,
    call_id: str,
    name: str,
    *,
    args: dict[str, Any] | None = None,
    patch: str | None = None,
) -> dict[str, Any]:
    if patch is not None:
        return _line(
            ordinal,
            "response_item",
            {"type": "custom_tool_call", "call_id": call_id, "name": name, "input": patch},
        )
    return _line(
        ordinal,
        "response_item",
        {
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": json.dumps(args or {}),
        },
    )


def _output(ordinal: int, call_id: str, output: Any, *, custom: bool = False) -> dict[str, Any]:
    kind = "custom_tool_call_output" if custom else "function_call_output"
    return _line(ordinal, "response_item", {"type": kind, "call_id": call_id, "output": output})


def _write_jsonl(path: Path, lines: list[dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _codex_log(root: Path, session: str = "codex-s1") -> Path:
    lines = [
        _line(0, "session_meta", {"session_id": session, "source": "cli"}),
        _call(1, "c1", "shell", args={"command": ["bash", "-lc", "uv run pytest -q"]}),
        _output(2, "c1", "Process exited with code 0"),
        _call(
            3,
            "c2",
            "apply_patch",
            patch="*** Begin Patch\n*** Update File: src/app.py\n*** End Patch\n",
        ),
        _output(4, "c2", "Success"),
        _call(5, "c3", "read_file", args={"path": "README.md"}),
        _output(6, "c3", "# readme"),
        _call(
            7,
            "c4",
            "update_plan",
            args={
                "plan": [
                    {"step": "ship it", "status": "in_progress"},
                    {"step": "done", "status": "completed"},
                ]
            },
        ),
        _call(
            8,
            "c5",
            "shell",
            args={
                "command": "export API_TOKEN=ghp_CANARYSECRET1234567890 && "
                "curl -H 'Authorization: Bearer tok' example.com"
            },
        ),
        _output(9, "c5", "Process exited with code 1"),
        _call(10, "c6", "web_search", args={"query": "CANARYPROMPT-1111"}),
        _output(11, "c6", "CANARYOUT-2222"),
    ]
    return _write_jsonl(root / "rollout-codex-s1.jsonl", lines)


def test_codex_packet_has_exact_observed_facts(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    root = tmp_path / "codex"
    root.mkdir()
    _codex_log(root)

    obj = handoff.build_packet("codex", root, "latest", str(project))

    files = {Path(item["path"]).name: item for item in obj["files"]}
    assert files["app.py"]["edited"] is True
    assert files["README.md"]["edited"] is False
    assert Path(files["app.py"]["path"]).is_absolute()
    assert obj["commands"] == [
        "uv run pytest -q",
        "export API_TOKEN=*** && curl -H 'Authorization: Bearer ***' example.com",
    ]
    assert obj["todos"] == [{"s": "in_progress", "t": "ship it"}]
    assert obj["test"]["key"] == "pytest"
    assert obj["test"]["ok"] is True
    assert obj["sid"] == "codex-s1"
    assert "agent=codex" in obj["source"]
    assert "apply_patch=edit" in obj["source"]
    assert "shell=command" in obj["source"]
    assert packet.passed(packet.validate(obj)), packet.validate(obj)


def test_build_sanitises_a_command_that_would_fail_validation(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    root = tmp_path / "codex"
    root.mkdir()
    _write_jsonl(
        root / "rollout-sanitise.jsonl",
        [
            _line(0, "session_meta", {"session_id": "s"}),
            _call(1, "c1", "shell", args={"command": "cat < in.txt > out.txt"}),
            _output(2, "c1", "Process exited with code 0"),
        ],
    )
    obj = handoff.build_packet("codex", root, "latest", str(project))
    assert "<" not in obj["commands"][0]
    assert ">" not in obj["commands"][0]
    assert packet.passed(packet.validate(obj)), packet.validate(obj)


def test_codex_packet_never_carries_message_text_or_secrets(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    root = tmp_path / "codex"
    root.mkdir()
    _codex_log(root)

    obj = handoff.build_packet("codex", root, "latest", str(project))
    blob = json.dumps(obj)
    for canary in ("CANARYPROMPT-1111", "CANARYOUT-2222", "ghp_CANARYSECRET1234567890", "tok"):
        assert canary not in blob
    assert "web_search" not in blob  # unknown tool names are ignored, not mapped


# --- hand-built OpenCode database -----------------------------------------


def _opencode_db(path: Path, session: str = "opencode-1") -> Path:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, "
        "time_created INTEGER, time_updated INTEGER)"
    )
    con.execute(
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
        "time_created INTEGER, data TEXT)"
    )
    con.execute(
        "CREATE TABLE part (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT)"
    )
    con.execute("INSERT INTO session VALUES (?, ?, ?, ?)", (session, None, 1000, 1000))
    parts: list[tuple[str, dict[str, Any], str, dict[str, Any] | None]] = [
        ("bash", {"command": "uv run pytest -q tests/test_o.py"}, "2 passed", {"exit": 0}),
        ("edit", {"filePath": "src/o.py"}, "", None),
        ("read", {"filePath": "docs/o.md"}, "text", None),
        ("todowrite", {"todos": [{"content": "write docs", "status": "pending"}]}, "", None),
        ("grep", {"pattern": "CANARYPROMPT-3333"}, "CANARYOUT-4444", None),
        (
            "bash",
            {"command": "export API_TOKEN=sk-CANARYSECRET1234567890"},
            "denied",
            {"exit": 1},
        ),
    ]
    for index, (tool, tool_input, output, metadata) in enumerate(parts):
        state: dict[str, Any] = {"status": "completed", "input": tool_input, "output": output}
        if metadata is not None:
            state["metadata"] = metadata
        data = json.dumps({"type": "tool", "tool": tool, "callID": f"c{index}", "state": state})
        con.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?)", (f"p{index}", session, 1000 + index, data)
        )
    con.commit()
    con.close()
    return path


def test_opencode_packet_uses_its_own_tool_names(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    db = _opencode_db(tmp_path / "opencode.db")

    obj = handoff.build_packet("opencode", db, "latest", str(project))

    files = {Path(item["path"]).name: item for item in obj["files"]}
    assert files["o.py"]["edited"] is True
    assert files["o.md"]["edited"] is False
    assert obj["todos"] == [{"s": "pending", "t": "write docs"}]
    assert obj["test"] == {"key": "pytest", "ok": True, "at": obj["test"]["at"]}
    assert obj["commands"][0].startswith("uv run pytest")
    assert "API_TOKEN=***" in obj["commands"][1]
    assert "agent=opencode" in obj["source"]
    assert "edit=edit" in obj["source"]
    assert packet.passed(packet.validate(obj)), packet.validate(obj)
    blob = json.dumps(obj)
    for canary in ("CANARYPROMPT-3333", "CANARYOUT-4444", "sk-CANARYSECRET1234567890"):
        assert canary not in blob


# --- hand-built Claude transcript -----------------------------------------


def _claude_transcript(root: Path, project: Path) -> Path:
    target = project / "claude.py"
    lines: list[dict[str, Any]] = [
        {
            "type": "assistant",
            "timestamp": _TS,
            "message": {
                "content": [
                    {"type": "text", "text": "CANARYPROMPT-5555"},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": str(target)},
                    },
                ]
            },
        },
        {
            "type": "assistant",
            "timestamp": _TS,
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "Edit",
                        "input": {"file_path": str(target)},
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "timestamp": _TS,
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t3",
                        "name": "Bash",
                        "input": {"command": "uv run pytest -q"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "timestamp": _TS,
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t3",
                        "content": [{"type": "text", "text": "Exit code 0"}],
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "timestamp": _TS,
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t4",
                        "name": "TodoWrite",
                        "input": {"todos": [{"content": "finish", "status": "pending"}]},
                    }
                ]
            },
        },
    ]
    return _write_jsonl(root / "claude-s1.jsonl", lines)


def test_claude_packet_from_hand_built_transcript(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    root = tmp_path / "claude"
    root.mkdir()
    _claude_transcript(root, project)

    obj = handoff.build_packet("claude", root, "claude-s1", str(project))

    assert [Path(item["path"]).name for item in obj["files"]] == ["claude.py"]
    assert obj["files"][0]["edited"] is True
    assert obj["commands"] == ["uv run pytest -q"]
    assert obj["todos"] == [{"s": "pending", "t": "finish"}]
    assert obj["test"]["key"] == "pytest"
    assert obj["test"]["ok"] is True
    assert "agent=claude" in obj["source"]
    assert "CANARYPROMPT-5555" not in json.dumps(obj)
    assert packet.passed(packet.validate(obj)), packet.validate(obj)


# --- import round trip -----------------------------------------------------


def _codex_packet(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    project = tmp_path / "proj"
    project.mkdir()
    root = tmp_path / "codex"
    root.mkdir()
    _codex_log(root)
    return handoff.build_packet("codex", root, "latest", str(project)), project


def test_import_round_trip_restores_once(tmp_path: Path) -> None:
    obj, project = _codex_packet(tmp_path)
    state_dir = tmp_path / "state"

    counts = handoff.import_packet(obj, state_dir, str(project))
    assert counts["files"] == 2
    stored = json.loads(checkpoint.checkpoint_path(state_dir, str(project)).read_text("utf-8"))
    assert stored["armed"] is True
    assert stored["cwd"] == checkpoint._norm(str(project))

    shown = checkpoint.show_text(state_dir, str(project))
    assert shown is not None
    assert "src/app.py" in shown or "app.py" in shown
    assert "uv run pytest -q" in shown

    event = {"session_id": "s", "source": "clear", "cwd": str(project)}
    first = checkpoint.handle_session_start(event, state_dir)
    assert first["hookSpecificOutput"]["additionalContext"] == shown
    assert checkpoint.handle_session_start(event, state_dir) == {}


def test_import_refuses_invalid_stale_and_wrong_version(tmp_path: Path) -> None:
    obj, project = _codex_packet(tmp_path)
    state_dir = tmp_path / "state"

    invalid = dict(obj) | {"commands": ["not masked: ghp_SECRET1234567890123456"]}
    with pytest.raises(ValueError, match="not valid"):
        handoff.import_packet(invalid, state_dir, str(project))

    stale = dict(obj) | {"saved_at": (datetime.now(UTC) - timedelta(hours=25)).isoformat()}
    with pytest.raises(ValueError, match="24 hours"):
        handoff.import_packet(stale, state_dir, str(project))
    assert handoff.import_packet(stale, state_dir, str(project), force=True)["files"] == 2

    wrong = dict(obj) | {"v": 1}
    with pytest.raises(ValueError, match="not valid"):
        handoff.import_packet(wrong, state_dir, str(project))


# --- render ----------------------------------------------------------------


def _minimal_packet() -> dict[str, Any]:
    return {
        "v": 0,
        "sid": "s",
        "created": _TS,
        "trigger": "checkpoint",
        "cwd": "/work/project",
        "saved_at": _TS,
        "armed": True,
        "files": [{"path": "/work/project/a.py", "lines": 3, "edited": True, "t": 1000.0}],
        "folds": [],
        "commands": ["uv run pytest -q"],
        "todos": [{"s": "pending", "t": "ship it"}],
        "test": {"key": "pytest", "ok": True, "at": _AT},
    }


def test_render_agents_md_exact_and_plain(tmp_path: Path) -> None:
    obj = _minimal_packet()
    clock = datetime.fromtimestamp(_AT).strftime("%H:%M")
    expected = "\n".join(
        [
            handoff._BEGIN,
            handoff._PROLOGUE,
            f"Last test run: pytest, passed (at {clock})",
            handoff._FILES_HEADER,
            "- /work/project/a.py | 3 lines | edited",
            handoff._COMMANDS_HEADER,
            "- uv run pytest -q",
            handoff._TODOS_HEADER,
            "- pending | ship it",
            handoff._END,
        ]
    )
    text, _dropped = handoff.render_packet(obj, "agents-md")
    assert text == expected
    assert len(text.encode("utf-8")) <= 3000

    plain, _dropped = handoff.render_packet(obj, "plain")
    assert plain == expected.split("\n", 1)[1].rsplit("\n", 1)[0]


def test_render_marks_stale_files_only_when_cwd_given(tmp_path: Path) -> None:
    old = tmp_path / "old.py"
    old.write_text("x", encoding="utf-8")
    gone = tmp_path / "gone.py"
    obj = {
        **_minimal_packet(),
        "saved_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
        "files": [
            {"path": str(old), "lines": 1, "edited": False, "t": 1000.0},
            {"path": str(gone), "lines": None, "edited": False, "t": 1001.0},
        ],
    }
    without, _ = handoff.render_packet(obj, "plain")
    assert "[missing]" not in without
    with_cwd, _ = handoff.render_packet(obj, "plain", str(tmp_path))
    assert "[changed since checkpoint]" in with_cwd
    assert "[missing]" in with_cwd


def test_render_stays_under_cap_and_drops_in_order() -> None:
    files = [
        {
            "path": f"/work/project/{'d' * 200}/f{index}.py",
            "lines": index,
            "edited": True,
            "t": index,
        }
        for index in range(20)
    ]
    obj = {
        **_minimal_packet(),
        "files": files,
        "commands": [f"echo {'c' * 200} {index}" for index in range(20)],
        "todos": [{"s": "pending", "t": "t" * 100} for _ in range(8)],
    }
    text, dropped = handoff.render_packet(obj, "agents-md")
    assert len(text.encode("utf-8")) <= 3000
    assert dropped["files"] > 0
    assert dropped["commands"] > 0


# --- AGENTS.md -------------------------------------------------------------


def test_agents_md_insert_replace_remove(tmp_path: Path) -> None:
    md = tmp_path / "AGENTS.md"
    original = "# Rules\n\nkeep this byte for byte\n"
    md.write_text(original, encoding="utf-8")

    first, _ = handoff.render_packet(_minimal_packet(), "agents-md")
    action, _size = handoff.install_agents_md(str(tmp_path), first)
    assert action == "wrote"
    text = md.read_text(encoding="utf-8")
    assert text.startswith(original)
    assert text.count(handoff._BEGIN) == 1
    assert text.count(handoff._END) == 1

    changed = _minimal_packet() | {"commands": ["echo second"]}
    second, _ = handoff.render_packet(changed, "agents-md")
    handoff.install_agents_md(str(tmp_path), second)
    replaced = md.read_text(encoding="utf-8")
    assert replaced.count(handoff._BEGIN) == 1
    assert replaced.startswith(original)
    assert replaced.endswith("\n")
    assert second in replaced and first not in replaced

    action, _size = handoff.install_agents_md(str(tmp_path), None)
    assert action == "removed"
    assert md.read_text(encoding="utf-8") == original


def test_agents_md_refuses_damaged_markers(tmp_path: Path) -> None:
    md = tmp_path / "AGENTS.md"
    md.write_text("# Rules\n" + handoff._BEGIN + "\nno end marker\n", encoding="utf-8")
    before = md.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="damaged"):
        handoff.install_agents_md(str(tmp_path), "block")
    assert md.read_text(encoding="utf-8") == before


# --- CLI -------------------------------------------------------------------


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cfxd", *args],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_cli_build_validate_import_show(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    root = tmp_path / "codex"
    root.mkdir()
    _codex_log(root)
    out = tmp_path / "packet.json"
    state_dir = tmp_path / "state"

    built = _cli(
        "packet",
        "build",
        "--agent",
        "codex",
        "--root",
        str(root),
        "--cwd",
        str(project),
        "--out",
        str(out),
    )
    assert built.returncode == 0, built.stderr
    assert built.stdout == ""
    assert "2 files" in built.stderr
    assert "CANARYSECRET" not in built.stderr

    valid = _cli("packet", "validate", str(out))
    assert valid.returncode == 0, valid.stdout

    imported = _cli(
        "checkpoint", "import", str(out), "--cwd", str(project), "--state-dir", str(state_dir)
    )
    assert imported.returncode == 0, imported.stderr
    assert "2 files" in imported.stdout
    assert str(project) not in imported.stdout

    shown = _cli("checkpoint", "show", "--cwd", str(project), "--state-dir", str(state_dir))
    assert shown.returncode == 0, shown.stderr
    assert "Working set" in shown.stdout


def test_cli_render_install_and_remove(tmp_path: Path) -> None:
    obj, project = _codex_packet(tmp_path)
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(obj), encoding="utf-8")
    md = project / "AGENTS.md"
    md.write_text("# Rules\n", encoding="utf-8")

    rendered = _cli("packet", "render", str(path))
    assert rendered.returncode == 0, rendered.stderr
    assert handoff._BEGIN in rendered.stdout
    assert len(rendered.stdout.encode("utf-8")) <= 3000

    installed = _cli("packet", "install-agents-md", "--cwd", str(project), "--packet", str(path))
    assert installed.returncode == 0, installed.stderr
    assert handoff._BEGIN in md.read_text(encoding="utf-8")

    removed = _cli("packet", "install-agents-md", "--cwd", str(project), "--remove")
    assert removed.returncode == 0, removed.stderr
    assert md.read_text(encoding="utf-8") == "# Rules\n"


def test_cli_import_refuses_stale_without_force(tmp_path: Path) -> None:
    obj, project = _codex_packet(tmp_path)
    obj = dict(obj) | {"saved_at": (datetime.now(UTC) - timedelta(hours=25)).isoformat()}
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(obj), encoding="utf-8")
    state_dir = tmp_path / "state"

    refused = _cli(
        "checkpoint", "import", str(path), "--cwd", str(project), "--state-dir", str(state_dir)
    )
    assert refused.returncode == 1
    assert "24 hours" in refused.stderr

    forced = _cli(
        "checkpoint",
        "import",
        str(path),
        "--cwd",
        str(project),
        "--state-dir",
        str(state_dir),
        "--force",
    )
    assert forced.returncode == 0, forced.stderr


def test_cli_install_refuses_damaged_block(tmp_path: Path) -> None:
    obj, project = _codex_packet(tmp_path)
    (project / "AGENTS.md").write_text(handoff._END + "\n", encoding="utf-8")
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(obj), encoding="utf-8")

    result = _cli("packet", "install-agents-md", "--cwd", str(project), "--packet", str(path))
    assert result.returncode == 1
    assert "damaged" in result.stderr


def test_build_without_a_log_fails_cleanly(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    result = _cli(
        "packet",
        "build",
        "--agent",
        "codex",
        "--root",
        str(root),
        "--cwd",
        str(tmp_path),
    )
    assert result.returncode == 1
    assert "no codex session log found" in result.stderr


def test_source_is_optional_and_validated() -> None:
    obj = _minimal_packet()
    assert packet.passed(packet.validate(obj))
    obj = dict(obj) | {"source": handoff.source_label("codex")}
    assert packet.passed(packet.validate(obj))
    bad = dict(obj) | {"source": 1}
    schema = next(check for check in packet.validate(bad) if check.name == "schema")
    assert schema.status == packet.FAIL


def test_import_preserves_source_and_reusing_carry_renderer(tmp_path: Path) -> None:
    obj, project = _codex_packet(tmp_path)
    state_dir = tmp_path / "state"
    handoff.import_packet(obj, state_dir, str(project))
    stored = json.loads(checkpoint.checkpoint_path(state_dir, str(project)).read_text("utf-8"))
    assert stored["source"] == obj["source"]
    text = checkpoint.show_text(state_dir, str(project))
    assert text is not None
    assert len(text.encode("utf-8")) <= carry.RENDER_CAP_BYTES
