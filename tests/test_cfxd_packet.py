"""cfxd packet tests: schema vs a real packet, every vector, staleness, masking, CLI."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cfxd import carry, checkpoint, packet
from cfxd import session_state as ss
from cfxd.handlers import clear

_ROOT = Path(__file__).resolve().parents[1]
_VECTORS = _ROOT / "docs" / "specs" / "vectors"
_SCHEMA_PATH = _ROOT / "docs" / "specs" / "context-packet-v0.schema.json"
_RULES = {
    "oversize-packet": "size",
    "unknown-field-packet": "schema",
    "bad-enum-kind": "schema",
    "control-character-path": "characters",
    "unmasked-secret-command": "masking",
    "too-many-files": "caps",
    "missing-required-cwd": "schema",
    "wrong-version": "schema",
    "note-too-long": "caps",
    "stale-marker-without-file": "provenance",
}


@pytest.fixture(autouse=True)
def _clean() -> Any:
    ss.reset()
    clear()
    checkpoint.reset()
    yield
    ss.reset()
    clear()
    checkpoint.reset()


def _schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _type_ok(name: str, value: Any) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def _schema_valid(node: dict[str, Any], value: Any, defs: dict[str, Any]) -> bool:
    """A tiny draft 2020-12 subset: ref, const, enum, type, required, properties, items, pattern."""
    if "$ref" in node:
        node = defs[node["$ref"].rsplit("/", 1)[-1]]
    if "const" in node and value != node["const"]:
        return False
    if "enum" in node and value not in node["enum"]:
        return False
    types = node.get("type")
    if types is not None:
        names = types if isinstance(types, list) else [types]
        if not any(_type_ok(name, value) for name in names):
            return False
    if isinstance(value, dict):
        for key in node.get("required", []):
            if key not in value:
                return False
        props = node.get("properties", {})
        if node.get("additionalProperties") is False and any(k not in props for k in value):
            return False
        for key, sub in props.items():
            if key in value and not _schema_valid(sub, value[key], defs):
                return False
    if isinstance(value, list) and "items" in node:
        if not all(_schema_valid(node["items"], item, defs) for item in value):
            return False
    if isinstance(value, str) and "pattern" in node:
        if re.search(node["pattern"], value) is None:
            return False
    return True


def test_schema_loads_and_matches_the_checker() -> None:
    schema = _schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(packet.REQUIRED)
    assert set(schema["properties"]) == set(packet.TOP_LEVEL)

    props = schema["properties"]
    assert props["v"]["const"] == 0
    assert props["trigger"]["const"] == packet.TRIGGER
    assert props["files"]["maxItems"] == ss.MAX_FILES
    assert props["folds"]["maxItems"] == ss.MAX_FOLDS
    assert props["commands"]["maxItems"] == ss.MAX_COMMANDS
    assert props["todos"]["maxItems"] == carry.MAX_TODOS
    assert props["note"]["maxLength"] == checkpoint.NOTE_MAX_CHARS

    defs = schema["$defs"]
    assert set(defs["file"]["required"]) == set(packet.FILE_FIELDS)
    assert set(defs["fold"]["required"]) == set(packet.FOLD_FIELDS)
    assert set(defs["todo"]["required"]) == set(packet.TODO_FIELDS)
    assert set(defs["test"]["required"]) == set(packet.TEST_FIELDS)
    assert set(defs["fold"]["properties"]["kind"]["enum"]) == packet.FOLD_KINDS
    assert set(defs["todo"]["properties"]["s"]["enum"]) == packet.TODO_STATUSES
    assert defs["todo"]["properties"]["t"]["maxLength"] == packet.TODO_MAX_BYTES
    for item in (defs["file"], defs["fold"], defs["todo"], defs["test"]):
        assert item["additionalProperties"] is False


def _seed_real_packet(tmp_path: Path) -> tuple[str, dict[str, Any]]:
    project = tmp_path / "proj"
    project.mkdir()
    path = project / "a.py"
    path.write_text("x", encoding="utf-8")
    old = datetime.now(UTC).timestamp() - 60
    os.utime(path, (old, old))
    session = "sess-schema"
    checkpoint.observe_cwd({"session_id": session, "cwd": str(project)})
    ss.observe_post_tool_use(
        {
            "session_id": session,
            "tool_name": "Edit",
            "tool_input": {"file_path": str(path)},
            "tool_response": {"file": {"numLines": 12, "totalLines": 12}},
        }
    )
    ss.observe_post_tool_use(
        {
            "session_id": session,
            "tool_name": "Bash",
            "tool_input": {"command": "export API_TOKEN=*** && uv run pytest -q"},
            "tool_response": {"exit_code": 0},
        }
    )
    ss.observe_post_tool_use(
        {
            "session_id": session,
            "tool_name": "TodoWrite",
            "tool_input": {"todos": [{"content": "ship it", "status": "in_progress"}]},
        }
    )
    ss.record_fold(
        session,
        tool="Bash",
        source="uv run pytest -q",
        obs_id="0123456789abcdef",
        obs_path=str(tmp_path / "obs" / "0123456789abcdef.txt"),
        original_bytes=4096,
        kind="test",
    )
    state_dir = tmp_path / "state"
    saved = checkpoint.handle_checkpoint_save(
        {"cwd": str(project), "note": "finish the schema"}, state_dir
    )
    assert "Checkpoint saved" in saved["summary"]
    obj = json.loads(checkpoint.checkpoint_path(state_dir, str(project)).read_text("utf-8"))
    return str(project), obj


def test_schema_accepts_a_real_checkpoint_packet(tmp_path: Path) -> None:
    cwd, obj = _seed_real_packet(tmp_path)
    schema = _schema()
    assert _schema_valid(schema, obj, schema["$defs"])
    checks = packet.validate(obj, cwd)
    assert packet.passed(checks), [c for c in checks if c.status == "FAIL"]
    assert {check.name for check in checks} == {
        "schema",
        "caps",
        "size",
        "provenance",
        "staleness",
        "masking",
        "characters",
    }


def _vector_paths() -> list[Path]:
    return sorted(_VECTORS.glob("*.json"))


def test_schema_and_checker_agree_on_every_vector() -> None:
    schema = _schema()
    for path in _vector_paths():
        obj = json.loads(path.read_text(encoding="utf-8"))
        verdict = next(c for c in packet.validate(obj) if c.name == "schema")
        assert _schema_valid(schema, obj, schema["$defs"]) == (verdict.status == packet.PASS), path


@pytest.mark.parametrize("path", _vector_paths(), ids=lambda p: p.name)
def test_every_vector_gets_its_expected_verdict(path: Path) -> None:
    checks = packet.validate(json.loads(path.read_text(encoding="utf-8")))
    failures = {check.name for check in checks if check.status == packet.FAIL}
    if path.name.startswith("valid-"):
        assert not failures, checks
        return
    rule = path.name.removeprefix("invalid-").removesuffix(".json")
    expected = _RULES[rule]
    assert failures == {expected}, (path.name, checks)


def _packet_with_files(paths: list[str]) -> dict[str, Any]:
    return {
        "v": 0,
        "sid": "s",
        "created": datetime.now(UTC).isoformat(),
        "trigger": "checkpoint",
        "cwd": "/work/project",
        "saved_at": datetime.now(UTC).isoformat(),
        "armed": True,
        "files": [
            {"path": path, "lines": 1, "edited": False, "t": 1761732000.0 + i}
            for i, path in enumerate(paths)
        ],
        "folds": [],
        "commands": [],
        "todos": [],
    }


def test_staleness_is_skipped_without_cwd_and_checked_with_one(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text("x", encoding="utf-8")
    old = datetime.now(UTC).timestamp() - 60
    os.utime(good, (old, old))
    obj = _packet_with_files([str(good)])
    skipped = next(c for c in packet.validate(obj) if c.name == "staleness")
    assert skipped.status == packet.SKIP
    passed = next(c for c in packet.validate(obj, str(tmp_path)) if c.name == "staleness")
    assert passed.status == packet.PASS

    future = datetime.now(UTC).timestamp() + 60
    os.utime(good, (future, future))
    changed = next(c for c in packet.validate(obj, str(tmp_path)) if c.name == "staleness")
    assert changed.status == packet.FAIL
    assert "1 changed" in changed.reason

    missing = _packet_with_files([str(tmp_path / "gone.py")])
    mark = next(c for c in packet.validate(missing, str(tmp_path)) if c.name == "staleness")
    assert mark.status == packet.FAIL
    assert "1 missing" in mark.reason


def test_masking_requires_stability_under_redaction() -> None:
    safe = {"commands": ["export API_TOKEN=***", "git status --short"]}
    good = next(c for c in packet.validate(_packet_with_files([]) | safe) if c.name == "masking")
    assert good.status == packet.PASS
    unsafe = _packet_with_files([])
    unsafe["commands"] = ["export API_TOKEN=ghp_SECRETCANARY1234567890"]
    check = next(c for c in packet.validate(unsafe) if c.name == "masking")
    assert check.status == packet.FAIL
    assert "ghp_" not in check.reason
    commands = (
        "uv run pytest -q",
        "psql -W hunter2 -c 'select 1'",
        "curl -H 'Authorization: Bearer x'",
    )
    for command in commands:
        once = ss.redact_command(command)
        assert ss.redact_command(once) == once


def test_render_size_is_checked_with_the_shipped_renderer() -> None:
    small = json.loads((_VECTORS / "valid-minimal.json").read_text(encoding="utf-8"))
    assert next(c for c in packet.validate(small) if c.name == "size").status == packet.PASS
    large = json.loads((_VECTORS / "invalid-oversize-packet.json").read_text(encoding="utf-8"))
    check = next(c for c in packet.validate(large) if c.name == "size")
    assert check.status == packet.FAIL
    assert "drop" in check.reason


def test_edge_cases_fail_closed() -> None:
    checks = packet.validate(["not", "an", "object"])
    assert not packet.passed(checks)
    assert checks[0].status == packet.FAIL
    no_list = _packet_with_files([]) | {"files": "nope"}
    assert next(c for c in packet.validate(no_list) if c.name == "schema").status == packet.FAIL


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cfxd", "packet", "validate", *args],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_valid_invalid_json_and_unreadable() -> None:
    good = _cli(str(_VECTORS / "valid-minimal.json"))
    assert good.returncode == 0, good.stderr
    assert "PASS schema:" in good.stdout
    assert "SKIP staleness:" in good.stdout

    bad = _cli(str(_VECTORS / "invalid-wrong-version.json"), "--json")
    assert bad.returncode == 1
    report = json.loads(bad.stdout)
    assert report["ok"] is False
    assert any(item["name"] == "schema" and item["status"] == "FAIL" for item in report["checks"])

    missing = _cli(str(_VECTORS / "does-not-exist.json"))
    assert missing.returncode == 2
    assert "cannot read" in missing.stderr
